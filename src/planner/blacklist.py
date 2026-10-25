"""Aufgaben-Titel, die nie wieder automatisch angelegt werden sollen.

Matching ist bewusst einfach und vorhersehbar: exakt auf den normalisierten Titel (klein,
getrimmt, Leerraum zusammengezogen). `tasks.add_task` prüft das vor jedem Anlegen – Aufrufer
sehen dasselbe `None` wie bei einem Duplikat über (source, source_ref).
"""

from __future__ import annotations

import re
import sqlite3

from . import changelog
from .changelog import now_iso


def normalize(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def is_blacklisted(conn: sqlite3.Connection, title: str) -> bool:
    row = conn.execute("SELECT 1 FROM blacklist WHERE title_norm = ?", (normalize(title),)).fetchone()
    return row is not None


def add(conn: sqlite3.Connection, title: str, *, actor: str) -> int | None:
    """Sperrt einen Titel dauerhaft. Gibt None zurück, wenn er schon gesperrt ist."""
    norm = normalize(title)
    if not norm:
        return None
    try:
        cur = conn.execute(
            "INSERT INTO blacklist(title_norm, title, created_at) VALUES (?, ?, ?)",
            (norm, title.strip(), now_iso()),
        )
    except sqlite3.IntegrityError:
        return None
    conn.commit()
    bid = int(cur.lastrowid)
    changelog.log(conn, actor=actor, action="blacklist.added", entity="blacklist", entity_id=bid,
                  detail={"title": title})
    return bid


def remove(conn: sqlite3.Connection, blacklist_id: int, *, actor: str) -> str:
    """Entfernt einen Eintrag und gibt seinen Titel zurück. KeyError, wenn es ihn nicht gibt."""
    row = conn.execute("SELECT title FROM blacklist WHERE id = ?", (blacklist_id,)).fetchone()
    if row is None:
        raise KeyError(blacklist_id)
    conn.execute("DELETE FROM blacklist WHERE id = ?", (blacklist_id,))
    conn.commit()
    changelog.log(conn, actor=actor, action="blacklist.removed", entity="blacklist", entity_id=blacklist_id,
                  detail={"title": row["title"]})
    return row["title"]


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM blacklist ORDER BY title_norm").fetchall()
