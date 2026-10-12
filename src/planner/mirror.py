"""Spiegelt externe Termine (z. B. Stundenplan) in einen eigenen Google-Kalender.

Idempotent: vergleicht den Soll-Zustand (`external_events`) mit dem, was bereits gespiegelt
wurde (`mirror`), und legt an / aktualisiert / löscht nur die Differenz. Es werden
ausschließlich selbst angelegte Events im Spiegel-Kalender verändert.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from . import changelog
from .calendar_client import CalendarClient

MIRROR_OWNER = "planner_mirror"


@dataclass
class MirrorResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0


def _fingerprint(row: sqlite3.Row) -> str:
    raw = json.dumps(
        [row["title"], row["start"], row["end"], row["all_day"], row["location"], row["description"]]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _body(row: sqlite3.Row, source: str) -> dict:
    if row["all_day"]:
        start, end = {"date": row["start"]}, {"date": row["end"]}
    else:
        start, end = {"dateTime": row["start"]}, {"dateTime": row["end"]}
    return {
        "summary": row["title"],
        "location": row["location"] or None,
        "description": row["description"] or None,
        "start": start,
        "end": end,
        "extendedProperties": {"private": {MIRROR_OWNER: "1", "source": source, "uid": row["uid"]}},
    }


def mirror_source(
    conn: sqlite3.Connection,
    cal: CalendarClient,
    *,
    source: str,
    calendar_id: str,
    now: datetime | None = None,
) -> MirrorResult:
    """Spiegelt alle noch nicht beendeten, aktiven Termine von `source`."""
    now_utc = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    res = MirrorResult()
    wanted = conn.execute(
        "SELECT * FROM external_events WHERE source = ? AND state = 'active' AND "
        "(CASE WHEN all_day = 1 THEN substr(end,1,10) >= substr(?,1,10) ELSE end >= ? END)",
        (source, now_utc, now_utc),
    ).fetchall()
    mapped = {
        r["uid"]: r
        for r in conn.execute("SELECT * FROM mirror WHERE source = ?", (source,)).fetchall()
    }

    for row in wanted:
        fp = _fingerprint(row)
        m = mapped.pop(row["uid"], None)
        if m is None:
            ev = cal.insert_event(calendar_id, _body(row, source))
            conn.execute(
                "INSERT INTO mirror(source, uid, calendar_id, gcal_event_id, fingerprint) "
                "VALUES (?,?,?,?,?)",
                (source, row["uid"], calendar_id, ev["id"], fp),
            )
            res.created += 1
        elif m["fingerprint"] != fp:
            cal.update_event(calendar_id, m["gcal_event_id"], _body(row, source))
            conn.execute(
                "UPDATE mirror SET fingerprint = ? WHERE source = ? AND uid = ?",
                (fp, source, row["uid"]),
            )
            res.updated += 1
        else:
            res.unchanged += 1
        conn.commit()

    # Übrig = nicht mehr gewünscht (entfallen oder vorbei) → nur Entfallene löschen
    for uid, m in mapped.items():
        ext = conn.execute(
            "SELECT state FROM external_events WHERE source = ? AND uid = ?", (source, uid)
        ).fetchone()
        if ext is not None and ext["state"] == "active":
            continue  # nur vergangen: Google-Event bleibt als Historie stehen
        cal.remove_event(m["calendar_id"], m["gcal_event_id"])
        conn.execute("DELETE FROM mirror WHERE source = ? AND uid = ?", (source, uid))
        res.deleted += 1
        conn.commit()

    if res.created or res.updated or res.deleted:
        changelog.log(
            conn, actor="system", action="mirror.synced", entity="calendar", entity_id=calendar_id,
            detail={"source": source, "created": res.created, "updated": res.updated,
                    "deleted": res.deleted},
        )
    return res
