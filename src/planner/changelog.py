"""Change-Log: jede automatische oder manuelle Änderung wird nachvollziehbar protokolliert."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    entity: str,
    entity_id: str | int | None = None,
    detail: dict[str, Any] | None = None,
    reason: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO changelog(ts, actor, action, entity, entity_id, detail, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            now_iso(),
            actor,
            action,
            entity,
            None if entity_id is None else str(entity_id),
            json.dumps(detail, ensure_ascii=False) if detail is not None else None,
            reason,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM changelog ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
