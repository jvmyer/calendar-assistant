"""ILIAS → Aufgaben: neuen Eintrag lesen, Fristen finden, eintragen, melden.

Pro neuem Eintrag (Datei oder Forenbeitrag):
1. Inhalt laden und als Text lesen (PDF/Word/Excel), Kurs über den Breadcrumb bestimmen
2. Fristen suchen (`deadlines.py`); „bis zur nächsten Stunde“ über den Stundenplan auflösen
3. Plausible Fristen in der Zukunft werden **automatisch Aufgaben** (landen über den Planer als
   Google Task + Blocker im Kalender); der Nutzer bekommt eine Meldung mit „Verwerfen“-Button
4. Nur Vergangenes (typisch: Plan aus dem Vorsemester) wird **nie** eingetragen, nur gemeldet
5. Klingt es nach Aufgabe, aber ohne Frist → Meldung mit Button „Als Aufgabe anlegen“
6. Alles andere (Folien …) bleibt still

Der Ablauf ist idempotent: Schlägt die Meldung fehl, wird sie wiederholt, ohne Aufgaben doppelt
anzulegen.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import httpx

from . import areas, tasks
from .bot_logic import fmt_deadline
from .connectors.ilias import BASE, IliasAuthError, text_looks_like_task
from .courses import clean_course_name, next_session
from .deadlines import Deadline, find_deadlines
from .documents import UnsupportedDocument, extract_text, is_supported
from .rules import TZ

log = logging.getLogger("planner.ilias")
GENERIC_LABELS = {"abgabe", "deadline", "aufgabe", "upload", "submission", "due", "frist"}
MAX_TASKS_PER_ITEM = 8
MAX_ATTEMPTS = 3
MAX_ITEMS_PER_RUN = 10
HORIZON = timedelta(days=400)   # weiter entfernte „Fristen“ sind eher Tippfehler/Jahreszahlen
GUESS_LIMIT = timedelta(days=150)   # Datum ohne Jahr, weiter weg als das: Jahr zu unsicher für Automatik
# Alte Klausuren, Lösungen und Musterlösungen sind keine Aufgaben (Rechenaufgaben enthalten viele Daten)
NOT_ASSIGNMENT_TITLE = re.compile(r"klausur|probeklausur|exam\b|lösung|loesung|musterl|solution|altklausur", re.IGNORECASE)


class _Retry(Exception):
    """Vorübergehender Fehler (Netzwerk): im nächsten Lauf nochmal."""


@dataclass
class Created:
    task_id: int
    title: str
    deadline: str | None
    is_new: bool


@dataclass
class ItemResult:
    created: list[Created] = field(default_factory=list)
    stale: list[Deadline] = field(default_factory=list)
    unresolved: list[Deadline] = field(default_factory=list)
    uncertain: list[Deadline] = field(default_factory=list)   # Jahr geschätzt und weit weg → nur nachfragen


def _doc_name(title: str) -> str:
    return re.sub(r"(\.(?:pdf|docx|xlsx|txt|md|csv))+$", "", title, flags=re.IGNORECASE).strip()


def task_title(label: str, doc_title: str, course: str) -> str:
    label = label.strip() or "Aufgabe"
    base = f"{label}: {_doc_name(doc_title)}" if label.lower() in GENERIC_LABELS else label
    return (f"{base} ({course})" if course else base)[:200]


def _deadline_string(d: Deadline) -> str | None:
    if d.when is None:
        return None
    return d.when.date().isoformat() if d.date_only else d.when.isoformat(timespec="minutes")


def read_item_text(client, row: sqlite3.Row) -> str:
    """Text eines Eintrags: Überschrift, Titel, Beitragstext und – bei Dateien – der Dateiinhalt."""
    parts = [row["headline"] or "", row["title"] or "", row["body"] or ""]
    if row["obj_type"] == "file":
        try:
            data, ctype, name = client.download_file(row["ref_id"], accept=is_supported)
        except httpx.HTTPError as exc:
            raise _Retry(type(exc).__name__) from exc
        except ValueError:
            log.info("Datei zu groß, nur Titel ausgewertet")
        else:
            try:
                parts.append(extract_text(data, name or row["title"], ctype))
            except UnsupportedDocument:
                pass
            except Exception as exc:  # noqa: BLE001 – defekte Datei darf den Lauf nicht stoppen
                log.warning("Dokument nicht lesbar: %s", type(exc).__name__)
    return "\n".join(p for p in parts if p)


def _resolve(conn: sqlite3.Connection, found: list[Deadline], course: str, posted: datetime,
             now: datetime) -> tuple[list[Deadline], list[Deadline], list[Deadline], list[Deadline]]:
    """(zukünftig, vergangen, unauflösbar, unsicher) – relative Fristen über den Stundenplan aufgelöst."""
    future: list[Deadline] = []
    past: list[Deadline] = []
    unresolved: list[Deadline] = []
    uncertain: list[Deadline] = []
    for d in found:
        if d.relative == "next_session":
            session = next_session(conn, course, posted)
            if session is None:
                unresolved.append(d)
                continue
            when = datetime.fromisoformat(session["start"]).astimezone(TZ)
            d = replace(d, when=when, date_only=False, relative=None, past=when < now)
        if d.past:
            past.append(d)
        elif d.when is not None and d.year_guessed and d.when - posted > GUESS_LIMIT:
            uncertain.append(d)
        elif d.when is not None and d.when - now <= HORIZON:
            future.append(d)
    return future, past, unresolved, uncertain


def _ensure_task(conn: sqlite3.Connection, *, title: str, deadline: str | None, area_id: int | None,
                 ref: str) -> Created | None:
    """None, wenn der Titel auf der Blacklist steht – dann wird stillschweigend nichts angelegt."""
    task_id = tasks.add_task(conn, title=title, source="ilias", source_ref=ref, actor="ilias",
                             area_id=area_id, deadline=deadline)
    if task_id is not None:
        return Created(task_id, title, deadline, True)
    row = conn.execute("SELECT id FROM tasks WHERE source = 'ilias' AND source_ref = ?", (ref,)).fetchone()
    return Created(int(row["id"]), title, deadline, False) if row else None


def analyse(conn: sqlite3.Connection, client, row: sqlite3.Row, now: datetime) -> tuple[ItemResult, str]:
    """Liest einen Eintrag und legt Aufgaben an. Rückgabe: (Ergebnis, Kursname)."""
    posted = datetime.fromisoformat(row["ts"]).astimezone(TZ)
    text = read_item_text(client, row)
    try:
        course = clean_course_name(client.course_of(row["obj_type"], row["ref_id"]))
    except IliasAuthError:
        raise
    except httpx.HTTPError as exc:
        raise _Retry(type(exc).__name__) from exc
    except Exception:  # noqa: BLE001 – Kurs ist nur Zusatzinfo
        course = ""

    found = [] if NOT_ASSIGNMENT_TITLE.search(row["title"] or "") else find_deadlines(text, posted=posted, now=now)
    future, past, unresolved, uncertain = _resolve(conn, found, course, posted, now)
    result = ItemResult(stale=past, unresolved=unresolved, uncertain=uncertain)
    area_id = areas.ensure_path(conn, f"Uni/{course.replace('/', '-')}", actor="ilias") if course else None

    for i, d in enumerate(future[:MAX_TASKS_PER_ITEM]):
        created = _ensure_task(
            conn, title=task_title(d.label, row["title"], course), deadline=_deadline_string(d),
            area_id=area_id, ref=f"{row['news_id']}:{i}")
        if created is not None:
            result.created.append(created)
    for j, d in enumerate(unresolved[:1]):   # Frist nicht auflösbar: Aufgabe trotzdem anlegen (nie verlieren)
        created = _ensure_task(
            conn, title=task_title(d.label, row["title"], course), deadline=None, area_id=area_id,
            ref=f"{row['news_id']}:r{j}")
        if created is not None:
            result.created.append(created)
    return result, course


def build_message(row: sqlite3.Row, res: ItemResult, course: str) -> tuple[str, list[list[tuple[str, str]]]] | None:
    """Meldungstext + Buttons; None = nichts zu melden."""
    posted = datetime.fromisoformat(row["ts"]).astimezone(TZ)
    source = f"{row['title']} ({row['author'].split(',')[0]}, {posted:%d.%m.})"
    where = f" · {course}" if course else ""
    link = f"{BASE}goto.php/{row['obj_type']}/{row['ref_id']}"
    ilt = [("➕ Als Aufgabe anlegen", f"ilt:{row['news_id']}")]
    if res.created:
        lines = [f"📚 ILIAS{where}"]
        for c in res.created[:MAX_TASKS_PER_ITEM]:
            when = f"bis {fmt_deadline(c.deadline)}" if c.deadline else "ohne Frist"
            lines.append(f"#{c.task_id} {c.title} – {when}")
        lines.append(f"Quelle: {source}")
        if res.unresolved:
            lines.append("⚠️ „Bis zur nächsten Stunde“: kein passender Stundenplan-Termin gefunden – "
                         "ohne Frist angelegt, bitte per /frist ergänzen.")
        if res.stale:
            lines.append(f"ℹ️ {len(res.stale)} Termin(e) in der Vergangenheit ignoriert.")
        if res.uncertain:
            lines.append(f"ℹ️ {len(res.uncertain)} Datum/Daten ohne Jahr, weit in der Zukunft – nicht eingetragen.")
        lines.append(link)
        buttons = [[(f"🗑 #{c.task_id} verwerfen", f"drop:{c.task_id}"), (f"🚫 #{c.task_id} nie wieder", f"blk:{c.task_id}")]
                  for c in res.created[:4]]
        return "\n".join(lines), buttons
    if res.uncertain:
        d = res.uncertain[0]
        text = (f"⚠️ ILIAS{where}: „{row['title']}“ nennt Daten ohne Jahr, weit in der Zukunft "
                f"(z. B. „{d.evidence[:80]}“ → {d.when:%d.%m.%Y}). Jahr unklar, nichts eingetragen – bitte prüfen.\n{link}")
        return text, [ilt]
    if res.stale:
        d = res.stale[0]
        text = (f"⚠️ ILIAS{where}: „{row['title']}“ nennt nur Termine in der Vergangenheit "
                f"(z. B. „{d.label or 'Abgabe'}“ am {d.when:%d.%m.%Y}) – vermutlich ein Plan aus dem Vorsemester. "
                f"Nichts eingetragen.\n{link}")
        return text, [ilt]
    if text_looks_like_task(row["title"], row["headline"] or "", row["body"] or ""):
        body = f"\n{row['body'][:200]}" if row["body"] else ""
        text = (f"📚 Neu in ILIAS – mögliche Aufgabe (keine Frist erkannt){where}\n{row['title']}{body}\n"
                f"{source}\n{link}")
        return text, [ilt]
    return None


def process_news(conn: sqlite3.Connection, client, notifier, now: datetime, *,
                 max_items: int = MAX_ITEMS_PER_RUN) -> int:
    """Alle neuen Einträge verarbeiten. Rückgabe: Anzahl abgeschlossener Einträge."""
    rows = conn.execute("SELECT * FROM ilias_news WHERE state = 'new' ORDER BY ts LIMIT ?", (max_items,)).fetchall()
    with_buttons = getattr(notifier, "send_with_buttons", None)
    finished = 0

    def settle(news_id: str) -> None:
        conn.execute("UPDATE ilias_news SET state = 'seen' WHERE news_id = ?", (news_id,))
        conn.commit()

    def fail(news_id: str, attempts: int) -> None:
        conn.execute("UPDATE ilias_news SET attempts = ? WHERE news_id = ?", (attempts, news_id))
        conn.commit()

    for row in rows:
        attempts = int(row["attempts"] or 0)
        try:
            res, course = analyse(conn, client, row, now)
        except _Retry as exc:
            log.warning("ILIAS-Eintrag %s: vorübergehender Fehler (%s)", row["news_id"], exc)
            if attempts + 1 >= MAX_ATTEMPTS:
                settle(row["news_id"])
            else:
                fail(row["news_id"], attempts + 1)
            continue
        msg = build_message(row, res, course)
        if msg is None:
            settle(row["news_id"])
            finished += 1
            continue
        text, buttons = msg
        ok = with_buttons(text, buttons) if with_buttons else notifier.send(text)
        if ok is not False or attempts + 1 >= MAX_ATTEMPTS:
            settle(row["news_id"])
            finished += 1
        else:
            fail(row["news_id"], attempts + 1)   # Aufgaben existieren schon; nur die Meldung wird wiederholt
    return finished
