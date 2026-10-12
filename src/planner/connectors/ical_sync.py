"""Generischer iCal-Abruf mit Änderungserkennung.

Der Feed wird gegen den zuletzt gespeicherten Stand verglichen (per UID). Änderungen an
Terminen, die noch nicht vorbei sind, landen im Change-Log und werden zurückgegeben,
damit Planer und Bot reagieren können. Der allererste Abruf ist ein stiller Import.
"""

from __future__ import annotations

import sqlite3
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from icalendar import Calendar

from .. import changelog
from ..changelog import now_iso
from ..db import get_meta, set_meta

MIN_INTERVAL_S = 30 * 60  # Raumzeit verlangt min. 30 Minuten zwischen zwei Abrufen


@dataclass
class Event:
    uid: str
    title: str
    start: str
    end: str
    all_day: bool = False
    location: str = ""
    description: str = ""


@dataclass
class SyncResult:
    added: list[Event] = field(default_factory=list)
    changed: list[tuple[Event, Event]] = field(default_factory=list)  # (alt, neu)
    removed: list[Event] = field(default_factory=list)
    initial: bool = False
    skipped: bool = False


def parse_ics(data: bytes | str) -> list[Event]:
    cal = Calendar.from_ical(data)
    events: list[Event] = []
    for comp in cal.walk("VEVENT"):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        dtstart = comp.decoded("DTSTART")
        dtend = comp.decoded("DTEND") if comp.get("DTEND") else dtstart
        all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)
        if all_day:
            start, end = dtstart.isoformat(), dtend.isoformat()
        else:
            start = dtstart.astimezone(UTC).isoformat(timespec="seconds")
            end = dtend.astimezone(UTC).isoformat(timespec="seconds")
        events.append(
            Event(
                uid=str(comp.get("UID")),
                title=str(comp.get("SUMMARY", "")).strip(),
                start=start,
                end=end,
                all_day=all_day,
                location=str(comp.get("LOCATION", "")).strip(),
                description=str(comp.get("DESCRIPTION", "")).strip(),
            )
        )
    return stable_keys(events)


def stable_keys(events: list[Event]) -> list[Event]:
    """Ersetzt die UID durch Titel+Start. Raumzeit nummeriert UIDs fortlaufend durch
    (`44532_159@…` → `44532_162@…`), sobald irgendwo ein Termin dazukommt – sie sind nicht stabil."""
    seen: dict[str, int] = {}
    for ev in events:
        base = f"{ev.title}|{ev.start}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        ev.uid = base if n == 0 else f"{base}#{n}"
    return events


def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "calendar-assistant/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        uid=row["uid"], title=row["title"], start=row["start"], end=row["end"],
        all_day=bool(row["all_day"]), location=row["location"] or "",
        description=row["description"] or "",
    )


def _same(a: Event, b: Event) -> bool:
    return (a.title, a.start, a.end, a.location) == (b.title, b.start, b.end, b.location)


def _is_upcoming(ev: Event, now_utc: str) -> bool:
    return ev.end[:10] >= now_utc[:10] if ev.all_day else ev.end >= now_utc


PAIR_WINDOW_S = 14 * 24 * 3600  # Verschiebung = gleicher Titel innerhalb von 14 Tagen


def _start_dt(ev: Event) -> datetime:
    d = datetime.fromisoformat(ev.start)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _pair_moves(removed: list[Event], added: list[Event]) -> list[tuple[Event, Event]]:
    """Ordnet entfallene und neue Termine mit gleichem Titel einander zu (= verschoben)."""
    pairs: list[tuple[Event, Event]] = []
    free = list(added)
    for old in sorted(removed, key=lambda e: e.start):
        cands = [
            (abs((_start_dt(new) - _start_dt(old)).total_seconds()), i)
            for i, new in enumerate(free)
            if new.title == old.title
        ]
        cands = [c for c in cands if c[0] <= PAIR_WINDOW_S]
        if cands:
            _, i = min(cands)
            pairs.append((old, free.pop(i)))
    return pairs


def sync(
    conn: sqlite3.Connection,
    source: str,
    events: list[Event],
    *,
    now: datetime | None = None,
) -> SyncResult:
    """Vergleicht `events` (mit stabilen Schlüsseln) mit dem Stand und protokolliert Änderungen."""
    now_utc = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    ts = now_iso()
    known = {
        r["uid"]: r
        for r in conn.execute("SELECT * FROM external_events WHERE source = ?", (source,))
    }
    result = SyncResult(initial=not known)
    feed = {ev.uid: ev for ev in events}

    new_evs = [ev for uid, ev in feed.items() if uid not in known or known[uid]["state"] == "removed"]
    gone_evs = [
        _row_to_event(r) for uid, r in known.items() if uid not in feed and r["state"] == "active"
    ]
    edited = [
        (_row_to_event(known[uid]), ev)
        for uid, ev in feed.items()
        if uid in known and known[uid]["state"] == "active" and not _same(_row_to_event(known[uid]), ev)
    ]

    # Datenbank auf Feed-Stand bringen
    for ev in new_evs:
        conn.execute(
            "INSERT INTO external_events(source, uid, title, start, end, all_day, location, "
            "description, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source, uid) DO UPDATE SET title=excluded.title, start=excluded.start, "
            "end=excluded.end, all_day=excluded.all_day, location=excluded.location, "
            "description=excluded.description, state='active', last_seen=excluded.last_seen",
            (source, ev.uid, ev.title, ev.start, ev.end, int(ev.all_day), ev.location,
             ev.description, ts, ts),
        )
    for _, ev in edited:
        conn.execute(
            "UPDATE external_events SET title=?, start=?, end=?, all_day=?, location=?, "
            "description=?, last_seen=? WHERE source=? AND uid=?",
            (ev.title, ev.start, ev.end, int(ev.all_day), ev.location, ev.description, ts,
             source, ev.uid),
        )
    conn.execute(
        f"UPDATE external_events SET last_seen = ? WHERE source = ? AND uid IN "
        f"({','.join('?' * len(feed)) or 'NULL'})",
        (ts, source, *feed),
    )
    for ev in gone_evs:
        conn.execute(
            "UPDATE external_events SET state='removed' WHERE source=? AND uid=?", (source, ev.uid)
        )
    conn.commit()

    if result.initial:
        changelog.log(conn, actor=source, action="source.initial_import", entity="source",
                      entity_id=source, detail={"events": len(events)})
        return result

    # Nur Kommende melden; Verschiebungen zuordnen
    added = [e for e in new_evs if _is_upcoming(e, now_utc)]
    removed = [e for e in gone_evs if _is_upcoming(e, now_utc)]
    moved = _pair_moves(removed, added)
    moved_old = {o.uid for o, _ in moved}
    moved_new = {n.uid for _, n in moved}
    result.added = [e for e in added if e.uid not in moved_new]
    result.removed = [e for e in removed if e.uid not in moved_old]
    result.changed = moved + [
        (o, n) for o, n in edited if _is_upcoming(o, now_utc) or _is_upcoming(n, now_utc)
    ]

    for old, new in moved:
        changelog.log(conn, actor=source, action="event.moved", entity="event", entity_id=new.uid,
                      detail={"old": _detail(old), "new": _detail(new)})
    for old, new in edited:
        changelog.log(conn, actor=source, action="event.changed", entity="event",
                      entity_id=new.uid, detail={"old": _detail(old), "new": _detail(new)})
    for ev in result.added:
        changelog.log(conn, actor=source, action="event.added", entity="event",
                      entity_id=ev.uid, detail=_detail(ev))
    for ev in result.removed:
        changelog.log(conn, actor=source, action="event.removed", entity="event",
                      entity_id=ev.uid, detail=_detail(ev))
    return result


def sync_url(conn: sqlite3.Connection, source: str, url: str, *, force: bool = False) -> SyncResult:
    last = get_meta(conn, f"last_fetch:{source}")
    if last and not force:
        age = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
        if age < MIN_INTERVAL_S:
            return SyncResult(skipped=True)
    result = sync(conn, source, parse_ics(fetch(url)))
    set_meta(conn, f"last_fetch:{source}", datetime.now(UTC).isoformat(timespec="seconds"))
    return result


def _detail(ev: Event) -> dict:
    return {"title": ev.title, "start": ev.start, "end": ev.end, "location": ev.location}
