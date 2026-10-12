"""Aufgaben anlegen und abfragen (jede Änderung geht ins Change-Log)."""

from __future__ import annotations

import sqlite3

from . import changelog
from .changelog import now_iso


def add_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    source: str,
    actor: str,
    source_ref: str | None = None,
    area_id: int | None = None,
    deadline: str | None = None,
    estimate_min: int | None = None,
) -> int | None:
    """Legt eine Aufgabe an. Gibt None zurück, wenn (source, source_ref) schon existiert."""
    ts = now_iso()
    try:
        cur = conn.execute(
            "INSERT INTO tasks(title, area_id, source, source_ref, deadline, estimate_min, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, area_id, source, source_ref, deadline, estimate_min, ts, ts),
        )
    except sqlite3.IntegrityError:
        return None
    conn.commit()
    task_id = int(cur.lastrowid)
    changelog.log(
        conn,
        actor=actor,
        action="task.created",
        entity="task",
        entity_id=task_id,
        detail={"title": title, "source": source, "deadline": deadline, "estimate_min": estimate_min},
    )
    return task_id


def set_status(conn: sqlite3.Connection, task_id: int, status: str, *, actor: str) -> None:
    if status not in {"open", "done", "dropped"}:
        raise ValueError(f"unbekannter Status: {status}")
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), task_id)
    )
    conn.commit()
    changelog.log(
        conn,
        actor=actor,
        action="task.status",
        entity="task",
        entity_id=task_id,
        detail={"from": row["status"], "to": status},
    )


def open_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tasks WHERE status = 'open' ORDER BY id").fetchall()
