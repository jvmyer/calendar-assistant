"""Gemeinsames für Postfächer (HKA über OWA, Gmail): Kopfdaten und Erkennung neuer Mails."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_AGE_DAYS = 14   # ältere Mails nie als „neu“ behandeln


@dataclass(frozen=True)
class MailHeader:
    item_id: str
    received: datetime
    sender: str
    subject: str

@dataclass
class InboxSyncResult:
    initial: bool = False
    stored: int = 0
    new: int = 0


def sync_inbox(conn: sqlite3.Connection, headers: list[MailHeader], *, now: datetime,
               source: str = "hka") -> InboxSyncResult:
    """Speichert Kopfdaten (keine Inhalte); der erste Lauf je Postfach ist ein stiller Erstimport (alles „seen“)."""
    from .. import changelog
    from ..db import get_meta, set_meta

    key = "mail_baselined" if source == "hka" else f"{source}_baselined"
    result = InboxSyncResult(initial=get_meta(conn, key) != "1")
    known = {r[0] for r in conn.execute("SELECT item_id FROM mails WHERE source = ?", (source,))}
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    stamp = changelog.now_iso()
    for h in sorted(headers, key=lambda m: m.received):
        if h.item_id in known:
            continue
        fresh = not result.initial and h.received >= cutoff
        conn.execute(
            "INSERT INTO mails(item_id, source, received, sender, subject, first_seen, state) VALUES (?,?,?,?,?,?,?)",
            (h.item_id, source, h.received.isoformat(), h.sender, h.subject, stamp, "new" if fresh else "seen"))
        result.stored += 1
        result.new += fresh
    conn.commit()
    if result.initial:
        set_meta(conn, key, "1")
    return result
