"""HKA-Mail → Aufgaben: neue Mail vom lokalen Modell lesen lassen, eintragen, melden.

Pro neuer Mail:
1. Text über OWA laden (nur im Speicher, nie in der Datenbank)
2. Lokales Modell (`llm.py`) urteilt: aufgabe | vorschlag | nichts, und kopiert die Frist-Stelle
3. Frist im Code bestimmen (`deadlines.resolve_phrase`); findet sich keine, regelbasiert im ganzen Text
4. „aufgabe“ → **automatisch Aufgabe** (mit oder ohne Frist – Aufgaben gehen nie verloren),
   Meldung mit „Verwerfen“-Button
5. „vorschlag“ → Vorschlag; kommt nur nach Bestätigung in den Kalender (Meldung über den Planer)
6. „nichts“ → still

Idempotent wie der ILIAS-Ablauf: Fehler (OWA, Modell) → im nächsten Lauf nochmal; nach
`MAX_ATTEMPTS` Fehlversuchen wird die Mail trotzdem gemeldet, damit nichts untergeht.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from . import feedback, proposals, tasks
from .bot_logic import fmt_deadline
from .deadlines import Deadline, find_deadlines, resolve_phrase
from .llm import LlmError, MailVerdict
from .rules import TZ

log = logging.getLogger("planner.mail")
MAX_ATTEMPTS = 3
MAX_ITEMS_PER_RUN = 5    # das Modell braucht ~30 s pro Mail; der Dienstlauf soll nicht ewig blockieren
HORIZON = timedelta(days=400)


class _Retry(Exception):
    """Vorübergehender Fehler (Netzwerk, Modell): im nächsten Lauf nochmal."""


@dataclass
class MailResult:
    verdict: MailVerdict
    deadline: str | None = None
    task_id: int | None = None
    proposal_id: int | None = None


def _deadline_string(d: Deadline) -> str:
    assert d.when is not None
    return d.when.date().isoformat() if d.date_only else d.when.isoformat(timespec="minutes")


def find_deadline(verdict: MailVerdict, subject: str, body: str, *, posted: datetime, now: datetime) -> str | None:
    """Frist aus der vom Modell kopierten Stelle; sonst die früheste plausible im ganzen Text."""
    d = resolve_phrase(verdict.deadline_text, posted=posted, now=now)
    if d is None:
        future = [x for x in find_deadlines(f"{subject}\n{body}", posted=posted, now=now)
                  if x.when is not None and not x.past and x.when - now <= HORIZON]
        d = future[0] if future else None
    return _deadline_string(d) if d else None


def _title(verdict: MailVerdict, subject: str) -> str:
    return (verdict.title or subject or "Mail beantworten").strip()[:200]


def analyse(conn: sqlite3.Connection, client, llm, row: sqlite3.Row, now: datetime) -> MailResult:
    posted = datetime.fromisoformat(row["received"]).astimezone(TZ)
    try:
        body = client.get_body(row["item_id"])
        verdict = llm.classify_mail(row["sender"] or "", row["subject"] or "", body, today=now.astimezone(TZ).date(),
                                    examples=feedback.example_messages(feedback.examples(conn, row["sender"] or "")))
    except (httpx.HTTPError, LlmError, ValueError) as exc:
        raise _Retry(type(exc).__name__) from exc
    res = MailResult(verdict)
    if verdict.kind == "nichts":
        return res
    res.deadline = find_deadline(verdict, row["subject"] or "", body, posted=posted, now=now)
    ref = row["item_id"]
    note = f"Mail von {row['sender']}: {row['subject']}"[:300]
    if verdict.kind == "aufgabe":
        task_id = tasks.add_task(conn, title=_title(verdict, row["subject"]), source="mail", source_ref=ref,
                                 actor="mail", deadline=res.deadline, estimate_min=verdict.effort_min)
        if task_id is None:   # schon angelegt (Meldung war fehlgeschlagen)
            task_id = int(conn.execute("SELECT id FROM tasks WHERE source = 'mail' AND source_ref = ?",
                                       (ref,)).fetchone()["id"])
        else:
            if verdict.priority != "mittel":
                tasks.set_priority(conn, task_id, feedback.PRIO_BOOST[verdict.priority], actor="mail")
            feedback.record(conn, task_id, item_id=ref, sender=row["sender"] or "", subject=row["subject"] or "",
                            received=row["received"], kind=verdict.kind, title=_title(verdict, row["subject"]),
                            deadline_text=verdict.deadline_text, deadline=res.deadline,
                            effort=verdict.effort_min, prio=verdict.priority, body=body)
        res.task_id = task_id
    else:
        res.proposal_id = proposals.add_proposal(
            conn, title=_title(verdict, row["subject"]), kind="task", source="mail", source_ref=ref, actor="mail",
            deadline=res.deadline, estimate_min=verdict.effort_min, note=note)
    return res


LABELS = {"hka": "📧 Aus HKA-Mail", "gmail": "✉️ Aus Gmail"}


def _label(row: sqlite3.Row) -> str:
    return LABELS.get(row["source"], "📧 Aus Mail")


def build_message(row: sqlite3.Row, res: MailResult, conn: sqlite3.Connection | None = None
                  ) -> tuple[str, list[list[tuple[str, str]]]] | None:
    """Nur Aufgaben werden hier gemeldet; Vorschläge meldet der Planer (mit Bestätigungs-Buttons).

    Mit `conn` (Einschätzung eingeschaltet): Karte mit Buttons für Art, Priorität, Aufwand und Frist.
    """
    if res.task_id is None:
        return None
    posted = datetime.fromisoformat(row["received"]).astimezone(TZ)
    source = f"Quelle: {row['subject']} ({row['sender']}, {posted:%d.%m.})"
    if conn is not None:
        text, buttons = feedback.card(conn, res.task_id, header=f"{_label(row)}➕ in den Kalender übernommen")
        return f"{text}\n{source}", buttons
    when = f"bis {fmt_deadline(res.deadline)}" if res.deadline else "ohne Frist (Frist ergänzen: /frist " \
        f"{res.task_id} 20.10.)"
    text = (f"{_label(row)}➕ in den Kalender übernommen\n"
            f"• #{res.task_id} {_title(res.verdict, row['subject'])} – {when}\n"
            f"Quelle: {row['subject']} ({row['sender']}, {posted:%d.%m.})")
    return text, [[(f"🗑 #{res.task_id} verwerfen", f"drop:{res.task_id}")]]


def process_mails(conn: sqlite3.Connection, client, llm, notifier, now: datetime, *,
                  max_items: int = MAX_ITEMS_PER_RUN, ask_feedback: bool = True, source: str = "hka") -> int:
    """Neue Mails verarbeiten (älteste zuerst). Rückgabe: Anzahl abgeschlossener Mails."""
    rows = conn.execute("SELECT * FROM mails WHERE state = 'new' AND source = ? ORDER BY received LIMIT ?",
                        (source, max_items)).fetchall()
    with_buttons = getattr(notifier, "send_with_buttons", None)
    finished = 0

    def settle(item_id: str, verdict: str | None) -> None:
        conn.execute("UPDATE mails SET state = 'seen', verdict = ? WHERE item_id = ?", (verdict, item_id))
        conn.commit()

    def fail(item_id: str, attempts: int) -> None:
        conn.execute("UPDATE mails SET attempts = ? WHERE item_id = ?", (attempts, item_id))
        conn.commit()

    for row in rows:
        attempts = int(row["attempts"] or 0)
        try:
            res = analyse(conn, client, llm, row, now)
        except _Retry as exc:
            log.warning("Mail konnte nicht ausgewertet werden (%s)", exc)
            if attempts + 1 >= MAX_ATTEMPTS:   # nie still verschwinden lassen
                notifier.send(f"⚠️ Mail konnte nicht automatisch gelesen werden – bitte selbst ansehen:\n"
                              f"{row['subject']} ({row['sender']})")
                settle(row["item_id"], None)
            else:
                fail(row["item_id"], attempts + 1)
            continue
        msg = build_message(row, res, conn if ask_feedback else None)
        if msg is None:
            settle(row["item_id"], res.verdict.kind)
            finished += 1
            continue
        text, buttons = msg
        ok = with_buttons(text, buttons) if with_buttons else notifier.send(text)
        if ok is not False or attempts + 1 >= MAX_ATTEMPTS:
            settle(row["item_id"], res.verdict.kind)
            finished += 1
        else:
            fail(row["item_id"], attempts + 1)   # Aufgabe existiert schon; nur die Meldung wird wiederholt
    return finished


# -- Probelauf: letzte Mails bewerten, nichts eintragen ------------------------------------------------
PRIO = (("🔴 hoch", timedelta(days=3)), ("🟠 mittel", timedelta(days=14)))


def priority(deadline: str | None, now: datetime) -> str:
    """Grobe Vorschau-Priorität nach Fristnähe (der echte Planer rechnet zusätzlich mit freier Zeit)."""
    if deadline is None:
        return "🟠 mittel"
    due = datetime.fromisoformat(deadline)
    due = due.replace(hour=23, minute=59, tzinfo=TZ) if len(deadline) == 10 else due
    left = due - now
    return next((label for label, limit in PRIO if left <= limit), "🟢 niedrig")


def review(conn: sqlite3.Connection, client, llm, headers, now: datetime) -> tuple[list[tuple], list[tuple], int, int]:
    """(Aufgaben, Vorschläge, Anzahl „nichts“, Fehler). Aufgaben: (Prio, Frist, Titel, Aufwand, Betreff, Absender)."""
    todo, maybe, nothing, failed = [], [], 0, 0
    for h in headers:
        try:
            body = client.get_body(h.item_id)
            v = llm.classify_mail(h.sender, h.subject, body, today=now.astimezone(TZ).date(),
                                  examples=feedback.example_messages(feedback.examples(conn, h.sender)))
        except (httpx.HTTPError, LlmError, ValueError):
            failed += 1
            continue
        if v.kind == "nichts":
            nothing += 1
            continue
        deadline = find_deadline(v, h.subject, body, posted=h.received, now=now)
        entry = (f"{feedback.PRIO_ICON[v.priority]} {v.priority}", deadline, _title(v, h.subject), v.effort_min, h.subject, h.sender)
        (todo if v.kind == "aufgabe" else maybe).append(entry)
    order = {f"{feedback.PRIO_ICON[q]} {q}": i for i, q in enumerate(feedback.PRIOS)}
    todo.sort(key=lambda e: (order[e[0]], e[1] or "9999"))
    return todo, maybe, nothing, failed


def format_review(todo, maybe, nothing: int, failed: int, n: int) -> str:
    lines = [f"📧 Probelauf: die letzten {n} Mails (nichts eingetragen)", ""]
    if todo:
        lines.append("Aufgaben:")
        for prio, deadline, title, effort, subject, sender in todo:
            when = f"bis {fmt_deadline(deadline)}" if deadline else "ohne Frist"
            mins = f" · ~{effort} Min." if effort else ""
            lines.append(f"{prio} · {title} – {when}{mins}\n    ↳ {subject} ({sender})")
    else:
        lines.append("Keine Aufgaben gefunden.")
    if maybe:
        lines += ["", "Vorschläge (nur nach Bestätigung):"]
        for _prio, deadline, title, _effort, subject, sender in maybe:
            when = f" – bis {fmt_deadline(deadline)}" if deadline else ""
            lines.append(f"💡 {title}{when}\n    ↳ {subject} ({sender})")
    lines += ["", f"Ohne To-do: {nothing}" + (f" · nicht lesbar: {failed}" if failed else "")]
    return "\n".join(lines)
