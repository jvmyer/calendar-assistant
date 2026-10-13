"""Vorschläge: „gut zu wissen“-Dinge (Barcamp, Helfer gesucht …), die keine Aufgabe sind.

Sie werden dem Nutzer gemeldet und kommen **nur nach Bestätigung** in den Kalender
(`accept`); sonst nie. Bestätigung läuft später über Telegram-Buttons, bis dahin per CLI.
Ein bestätigter Vorschlag wird zur Aufgabe (kind=task) oder zu einem Termin im Kalender
„Planer“ (kind=event).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import changelog, tasks
from .changelog import now_iso
from .rules import TZ


def add_proposal(
    conn: sqlite3.Connection,
    *,
    title: str,
    kind: str,
    source: str,
    actor: str,
    source_ref: str | None = None,
    start: str | None = None,
    end: str | None = None,
    deadline: str | None = None,
    estimate_min: int | None = None,
    area_id: int | None = None,
    note: str | None = None,
) -> int | None:
    """Legt einen Vorschlag an. None, wenn (source, source_ref) schon existiert."""
    if kind not in {"task", "event"}:
        raise ValueError(f"unbekannte Art: {kind}")
    if kind == "event" and not (start and end):
        raise ValueError("Ein Termin braucht Anfang und Ende")
    ts = now_iso()
    try:
        cur = conn.execute(
            "INSERT INTO proposals(kind, title, note, source, source_ref, start, end, deadline, "
            "estimate_min, area_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, title, note, source, source_ref, start, end, deadline, estimate_min, area_id, ts, ts),
        )
    except sqlite3.IntegrityError:
        return None
    conn.commit()
    pid = int(cur.lastrowid)
    changelog.log(conn, actor=actor, action="proposal.created", entity="proposal", entity_id=pid,
                  detail={"title": title, "kind": kind, "start": start})
    return pid


def _get(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise KeyError(proposal_id)
    return row


def _set_state(conn: sqlite3.Connection, proposal_id: int, state: str) -> None:
    conn.execute("UPDATE proposals SET state = ?, updated_at = ? WHERE id = ?", (state, now_iso(), proposal_id))
    conn.commit()


def accept(conn: sqlite3.Connection, proposal_id: int, *, actor: str) -> int | None:
    """Bestätigt einen Vorschlag. Gibt bei kind=task die neue Aufgaben-ID zurück.

    Ein Termin wird beim nächsten Planungslauf in den Kalender eingetragen (`materialize_events`).
    """
    row = _get(conn, proposal_id)
    if row["state"] != "pending":
        raise ValueError(f"Vorschlag #{proposal_id} ist schon {row['state']}")
    task_id = None
    if row["kind"] == "task":
        task_id = tasks.add_task(
            conn, title=row["title"], source="proposal", source_ref=str(proposal_id), actor=actor,
            area_id=row["area_id"], deadline=row["deadline"], estimate_min=row["estimate_min"],
        )
    _set_state(conn, proposal_id, "accepted")
    changelog.log(conn, actor=actor, action="proposal.accepted", entity="proposal", entity_id=proposal_id,
                  detail={"title": row["title"], "kind": row["kind"], "task_id": task_id})
    return task_id


def reject(conn: sqlite3.Connection, proposal_id: int, *, actor: str) -> None:
    row = _get(conn, proposal_id)
    if row["state"] != "pending":
        raise ValueError(f"Vorschlag #{proposal_id} ist schon {row['state']}")
    _set_state(conn, proposal_id, "rejected")
    changelog.log(conn, actor=actor, action="proposal.rejected", entity="proposal", entity_id=proposal_id,
                  detail={"title": row["title"]})


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM proposals WHERE state = 'pending' ORDER BY id").fetchall()


def materialize_events(conn: sqlite3.Connection, cal) -> int:
    """Trägt bestätigte Termine in den Kalender „Planer“ ein (nur bestätigte, nur einmal)."""
    rows = conn.execute(
        "SELECT * FROM proposals WHERE state = 'accepted' AND kind = 'event' AND gcal_event_id IS NULL"
    ).fetchall()
    if not rows:
        return 0
    cal_id = cal.ensure_planner_calendar()
    for row in rows:
        ev = cal.insert_event(cal_id, {
            "summary": row["title"],
            "description": row["note"] or "Vom Nutzer bestätigter Vorschlag (Planer).",
            "start": {"dateTime": row["start"]},
            "end": {"dateTime": row["end"]},
        })
        conn.execute("UPDATE proposals SET gcal_event_id = ?, updated_at = ? WHERE id = ?",
                     (ev["id"], now_iso(), row["id"]))
        conn.commit()
        changelog.log(conn, actor="planner", action="proposal.event_created", entity="proposal",
                      entity_id=row["id"], detail={"title": row["title"], "start": row["start"]},
                      reason="Vom Nutzer bestätigt")
    return len(rows)


def describe(row: sqlite3.Row) -> str:
    when = ""
    if row["kind"] == "event":
        s, e = (datetime.fromisoformat(row[k]).astimezone(TZ) for k in ("start", "end"))
        when = f" · {s:%a %d.%m. %H:%M}–{e:%H:%M}"
    elif row["deadline"]:
        when = f" · bis {row['deadline'][:16].replace('T', ' ')}"
    note = f"\n   {row['note']}" if row["note"] else ""
    kind = "Termin" if row["kind"] == "event" else "Aufgabe"
    return f"💡 Vorschlag #{row['id']} ({kind}): {row['title']}{when}{note}"


def take_unnotified(conn: sqlite3.Connection) -> tuple[str | None, list[int]]:
    """Meldungstext für noch nicht gemeldete Vorschläge + deren IDs (danach `mark_notified`)."""
    rows = conn.execute("SELECT * FROM proposals WHERE state = 'pending' AND notified = 0 ORDER BY id").fetchall()
    if not rows:
        return None, []
    lines = [describe(r) for r in rows]
    ids = [int(r["id"]) for r in rows]
    hint = "In den Kalender? `planner accept <Nr>` · verwerfen: `planner reject <Nr>` (ohne Antwort passiert nichts)"
    return "\n".join(lines) + "\n" + hint, ids


def mark_notified(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("UPDATE proposals SET notified = 1 WHERE id = ?", [(i,) for i in ids])
    conn.commit()
