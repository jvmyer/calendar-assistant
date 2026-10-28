"""Vorrat an „Aufgaben mit Zeitraum“: das Abhak-Kästchen direkt im Zeitfenster des Blockers.

Google lässt eine Task mit Uhrzeit nicht per API anlegen (Tasks-API kennt nur ein Datum, der dazugehörige
`focusTime`-Eintrag wird für Privatkonten abgelehnt, siehe docs/KONZEPT.md §5). Eine in der Google-Oberfläche
angelegte lässt sich aber verschieben, umbenennen, abhaken und wieder öffnen (live geprüft 2026-10-26).
Deshalb liegt in der Liste „Planer-Vorrat“ ein fester Vorrat solcher Aufgaben, der immer wieder verwendet wird:

- frei: geparkt am 01.01.2030 (im Alltag unsichtbar), Titel „Vorrat“
- belegt: steht 15 Min. lang am Anfang des Blockers (immer innerhalb), Titel = Aufgabe. Abhaken ist reine Erinnerung und wirkt
  NICHT auf die Aufgabe zurück (Nutzer 2026-10-20) – erledigt wird über die Zeit oder per Telegram.
- Einen Tag nach Ende des Blocks geht sie zurück auf den Parkplatz.

Task und Kalendereintrag gehören über den Link `tasks.google.com/task/<id>` zusammen (steht in der
Beschreibung des Eintrags und im `webViewLink` der Task). Titel nur über die Tasks-API ändern – die
Kalender-API ignoriert ihn bei diesen Einträgen still. Reihenfolge (live geprüft 2026-10-27): erst die Task
patchen, DANN den Eintrag verschieben – Google zieht das `due` der Task dabei selbst nach. Umgekehrt setzt ein
Tasks-Patch direkt nach dem Verschieben den Eintrag auf das alte `due` zurück (01.01.2030 08:00). `due` NIE selbst
setzen: das löscht den Zeitblock (Eintrag wird „cancelled“, die Task ganztägig). Der Eintrag ist „frei“
(transparent), blockiert also keine Planung. Wird der Vorrat knapp, meldet sich der Bot: Nachschub legt Claude per Chrome an.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import changelog
from .db import get_meta, set_meta
from .parsing import deadline_to_dt
from .rules import TZ

LIST_TITLE = "Planer-Vorrat"
META_LIST = "tasklist_id:vorrat"
PARK_TITLE = "Vorrat"
PARK_START = datetime(2030, 1, 1, 8, 0, tzinfo=TZ)
PARK_END = datetime(2030, 1, 1, 8, 30, tzinfo=TZ)
KEEP_AFTER_END = timedelta(days=1)     # abgelaufene Blöcke behalten ihr Kästchen noch einen Tag
SLOT = timedelta(minutes=15)          # Länge des Kästchens im Kalender (Nutzer 2026-10-26)
LOW_WATER = 10                         # darunter wird gewarnt
SEARCH_BACK = timedelta(days=60)       # hier sucht die Zuordnung neue Vorrat-Einträge im Kalender
SEARCH_UNTIL = datetime(2031, 1, 1, tzinfo=TZ)

PACE = 1.0                             # Sekunden zwischen zwei Schreibzugriffen (Google drosselt sonst mit 403)
RETRIES = (5, 15, 45)                  # Wartezeiten bei „Rate Limit Exceeded“ / 429 / 5xx
_sleep = time.sleep                    # Tests ersetzen das

_KEY = re.compile(r"tasks\.google\.com/task/([A-Za-z0-9_-]+)")


@dataclass
class PoolReport:
    adopted: int = 0
    assigned: int = 0
    parked: int = 0
    lost: int = 0
    free: int = 0
    missing: int = 0    # Blöcke, für die kein freies Kästchen mehr da war


def _key(text: str | None) -> str | None:
    m = _KEY.search(text or "")
    return m.group(1) if m else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _when(dt: datetime) -> dict[str, str]:
    return {"dateTime": dt.astimezone(TZ).isoformat(), "timeZone": "Europe/Berlin"}


class _Cancelled(Exception):
    """Zeitblock der Task wurde in Google gelöscht (Task ist dann ganztägig und als Kästchen unbrauchbar)."""


def _throttled(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status in (429, 500, 502, 503, 504) or (status == 403 and "ate Limit" in str(exc))


def _write(fn, *args):
    """Ein Schreibzugriff auf Google: mit Pause danach und Wiederholung, wenn Google drosselt
    (live 2026-10-27/28: schon wenige Verschiebungen direkt hintereinander → 403 „Rate Limit Exceeded“)."""
    for wait in (*RETRIES, None):
        try:
            result = fn(*args)
            _sleep(PACE)
            return result
        except Exception as exc:
            if wait is None or not _throttled(exc):
                raise
            _sleep(wait)


def _gone(exc: Exception) -> bool:
    return isinstance(exc, _Cancelled) or getattr(getattr(exc, "resp", None), "status", None) in (404, 410)


def _notes(task_id: int, deadline: str | None = None) -> str:
    lines = ["Zum Abhaken – reine Erinnerung, ändert nichts an der Planung."]
    if deadline:
        dl = deadline_to_dt(deadline)
        lines.append("Frist: " + dl.strftime("%d.%m.%Y" if len(deadline) == 10 else "%d.%m.%Y %H:%M"))
    return "\n".join(lines + [f"Aufgabe #{task_id} (Planer)"])


class _Writer:
    """Schreibt einen Vorrat-Eintrag nach Google und merkt sich den Stand (nur Differenzen werden geschrieben)."""

    def __init__(self, conn: sqlite3.Connection, cal, tc, list_id: str, primary: str, remote: dict[str, dict]):
        self.conn, self.cal, self.tc, self.list_id, self.primary = conn, cal, tc, list_id, primary
        self.remote = remote   # Stand der Tasks in Google (wird nach jedem Patch nachgeführt)

    def put(self, row: sqlite3.Row, *, block_id: int | None, title: str, notes: str, start: datetime,
            end: datetime, done: bool | None) -> bool:
        """`done=None` lässt den Haken, wie er ist. False = Eintrag existiert in Google nicht mehr (Zeile wird entfernt)."""
        remote = self.remote.setdefault(row["gtask_id"], {})
        try:
            body: dict[str, Any] = {}
            if row["title"] != title or (remote.get("notes") or "") != notes:
                body |= {"title": title, "notes": notes}
            if done is not None and (row["done"] is None or bool(row["done"]) != done):
                body |= {"status": "completed"} if done else {"status": "needsAction", "completed": None}
            if body:   # zuerst die Task, sonst setzt Google den verschobenen Eintrag auf ihr altes Datum zurück
                _write(self.tc.patch, self.list_id, row["gtask_id"], body)
                remote.update(body)
            stale = (remote.get("due") or "")[:10] != start.astimezone(TZ).date().isoformat()   # z. B. zurückgesprungen
            if row["start"] != _iso(start) or row["end"] != _iso(end) or stale:
                ev = _write(self.cal.patch_event, self.primary, row["event_id"], {"start": _when(start), "end": _when(end)})
                if (ev or {}).get("status") == "cancelled":
                    raise _Cancelled
                remote["due"] = f"{start.astimezone(TZ).date().isoformat()}T00:00:00.000Z"   # zieht Google selbst nach
        except Exception as exc:
            if not _gone(exc):
                raise
            _write(self.tc.delete, self.list_id, row["gtask_id"])   # ohne Eintrag nutzlos – nicht mit Aufgabentitel liegen lassen
            self.drop(row, "Vorrat-Eintrag in Google nicht mehr vorhanden")
            return False
        self.conn.execute(
            "UPDATE pool SET block_id = ?, title = ?, start = ?, end = ?, done = ? WHERE gtask_id = ?",
            (block_id, title, _iso(start), _iso(end), row["done"] if done is None else int(done), row["gtask_id"]))
        self.conn.commit()
        return True

    def park(self, row: sqlite3.Row) -> bool:
        return self.put(row, block_id=None, title=PARK_TITLE, notes="", start=PARK_START, end=PARK_END, done=False)

    def drop(self, row: sqlite3.Row, reason: str) -> None:
        self.conn.execute("DELETE FROM pool WHERE gtask_id = ?", (row["gtask_id"],))
        self.conn.commit()
        changelog.log(self.conn, actor="system", action="pool.lost", entity="pool", entity_id=row["gtask_id"],
                      detail={"block_id": row["block_id"]}, reason=reason)


def _adopt(conn: sqlite3.Connection, events: list[dict], new: dict[str, dict]) -> int:
    """Neue Tasks der Vorrat-Liste ihrem Kalendereintrag zuordnen (über den Task-Link)."""
    by_key = {_key(t.get("webViewLink")): gid for gid, t in new.items()}
    by_key.pop(None, None)
    adopted = 0
    for ev in events:
        gid = by_key.pop(_key(ev.get("description")), None)
        if gid is None:
            continue
        conn.execute("INSERT INTO pool(gtask_id, event_id) VALUES (?, ?)", (gid, ev["id"]))
        conn.commit()
        adopted += 1
    if adopted:
        changelog.log(conn, actor="system", action="pool.adopted", entity="pool", detail={"count": adopted},
                      reason="Neue Aufgaben mit Zeitraum im Vorrat")
    return adopted


def sync(conn: sqlite3.Connection, cal, tc, *, now: datetime | None = None) -> PoolReport:
    """Vorrat an die aktuellen Blöcke anpassen: jedes Zeitfenster bekommt ein Kästchen, der Rest parkt."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    report = PoolReport()
    list_id = tc.ensure_list(LIST_TITLE, META_LIST)
    primary = cal.primary_calendar_id()
    remote = {t["id"]: t for t in tc.list_tasks(list_id)}
    w = _Writer(conn, cal, tc, list_id, primary, remote)
    for row in conn.execute("SELECT * FROM pool").fetchall():
        if row["gtask_id"] not in remote:
            w.drop(row, "Vorrat-Task gelöscht")
            report.lost += 1
    events = cal.list_events(primary, now - SEARCH_BACK, SEARCH_UNTIL, event_types=["focusTime"])   # ohne gelöschte
    live = {ev["id"] for ev in events}
    for row in conn.execute("SELECT * FROM pool").fetchall():
        if row["event_id"] in live:
            continue
        ev = cal.get_event(primary, row["event_id"])   # erst bestätigen lassen – nie wegen eines leeren Abrufs löschen
        if ev is None or ev.get("status") == "cancelled":
            _write(tc.delete, list_id, row["gtask_id"])
            w.drop(row, "Zeitblock der Vorrat-Task in Google gelöscht")
            report.lost += 1
    known = {r["gtask_id"] for r in conn.execute("SELECT gtask_id FROM pool")}
    new = {gid: t for gid, t in remote.items() if gid not in known}
    if new:
        report.adopted = _adopt(conn, events, new)
    for row in conn.execute("SELECT gtask_id, done FROM pool").fetchall():   # Haken des Nutzers übernehmen
        ticked = int(remote.get(row["gtask_id"], {}).get("status") == "completed")
        if row["done"] is not None and row["done"] != ticked:
            conn.execute("UPDATE pool SET done = ? WHERE gtask_id = ?", (ticked, row["gtask_id"]))
    conn.commit()

    wanted = {b["id"]: b for b in conn.execute(
        "SELECT b.id, b.task_id, b.start, b.end, t.title, t.status, t.deadline FROM blocks b JOIN tasks t ON t.id = b.task_id "
        "WHERE b.state = 'active' AND b.end > ? AND t.status != 'dropped' ORDER BY b.start",
        (_iso(now - KEEP_AFTER_END),))}

    for row in conn.execute("SELECT * FROM pool WHERE block_id IS NOT NULL OR start IS NULL").fetchall():
        if row["block_id"] not in wanted and w.park(row):
            report.parked += 1

    taken = {r["block_id"]: r for r in conn.execute("SELECT * FROM pool WHERE block_id IS NOT NULL")}
    free = list(conn.execute("SELECT * FROM pool WHERE block_id IS NULL ORDER BY gtask_id"))
    for bid, b in wanted.items():
        start, end = datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])
        kw: dict[str, Any] = {"block_id": bid, "title": b["title"], "notes": _notes(b["task_id"], b["deadline"]),
                              "start": start, "end": min(start + SLOT, end)}   # nie über den Blocker hinaus
        row = taken.get(bid)
        if row is not None and w.put(row, **kw, done=True if b["status"] == "done" else None):
            continue   # Haken des Nutzers bleibt (reine Erinnerung)
        while free:    # frisch belegen und sauber öffnen; fehlt ein Eintrag in Google, das nächste nehmen
            if w.put(free.pop(0), **kw, done=b["status"] == "done"):
                report.assigned += 1
                break
        else:
            report.missing += 1
    report.free = conn.execute("SELECT COUNT(*) FROM pool WHERE block_id IS NULL").fetchone()[0]
    return report


def warning(conn: sqlite3.Connection, report: PoolReport, now: datetime) -> str | None:
    """Höchstens einmal pro Tag: Vorrat knapp oder Blöcke ohne Kästchen."""
    if report.free >= LOW_WATER and not report.missing:
        set_meta(conn, "pool_warned", "")
        return None
    today = now.astimezone(TZ).date().isoformat()
    if get_meta(conn, "pool_warned") == today:
        return None
    set_meta(conn, "pool_warned", today)
    text = f"📦 Vorrat an Aufgaben mit Zeitraum knapp: noch {report.free} frei."
    if report.missing:
        text += f" {report.missing} Blocker stehen gerade ohne Abhak-Kästchen im Kalender (geplant sind sie trotzdem)."
    return text + " Sag Claude: „Vorrat auffüllen“."


def retire_legacy_tasks(conn: sqlite3.Connection, tc) -> int:
    """Einmalig: die alten ganztägigen Google Tasks (Auffangnetz/Begleiter in der Liste „Planer“) entfernen –
    der Nutzer will keine Ganztags-Tasks mehr (2026-10-26). Die Aufgaben selbst bleiben unverändert."""
    rows = conn.execute("SELECT * FROM gtasks").fetchall()
    for row in rows:
        tc.delete(row["tasklist_id"], row["gtask_id"])
        conn.execute("DELETE FROM gtasks WHERE task_id = ?", (row["task_id"],))
        conn.commit()
    if rows:
        changelog.log(conn, actor="system", action="gtasks.retired", entity="gtasks", detail={"count": len(rows)},
                      reason="Ganztägige Tasks ersetzt durch Aufgaben mit Zeitraum aus dem Vorrat")
    return len(rows)


def collect_strays(tc, title: str = PARK_TITLE) -> int:
    """Nach dem Anlegen per Chrome: Tasks namens „Vorrat“ aus allen anderen Listen in die Vorrat-Liste holen.
    Der Zeitblock bleibt beim Listenwechsel erhalten (geprüft 2026-10-26). Braucht keine Datenbank."""
    lists = tc.all_lists()
    target = next((l["id"] for l in lists if l["title"] == LIST_TITLE), None) or tc.create_list(LIST_TITLE)
    moved = 0
    for lst in lists:
        if lst["id"] == target:
            continue
        for t in tc.list_tasks(lst["id"]):
            if t.get("title") == title and t.get("status") != "completed":
                tc.move(lst["id"], t["id"], target)
                moved += 1
    return moved
