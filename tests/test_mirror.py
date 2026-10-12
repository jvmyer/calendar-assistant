from datetime import UTC, datetime

import pytest

from planner.connectors.ical_sync import Event, sync
from planner.db import connect
from planner.mirror import mirror_source

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


class FakeCal:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.n = 0

    def insert_event(self, cal_id, body):
        self.n += 1
        self.events[f"g{self.n}"] = body
        return {"id": f"g{self.n}"}

    def update_event(self, cal_id, event_id, body):
        self.events[event_id] = body
        return {"id": event_id}

    def remove_event(self, cal_id, event_id):
        self.events.pop(event_id, None)


def ev(uid, start="2026-10-13T12:00:00+00:00", end="2026-10-13T13:30:00+00:00", loc="E-1"):
    return Event(uid=uid, title="V " + uid, start=start, end=end, location=loc)


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


def go(conn, cal, *events):
    sync(conn, "raumzeit", list(events), now=NOW)
    return mirror_source(conn, cal, source="raumzeit", calendar_id="cal", now=NOW)


def test_create_update_delete_cycle(conn):
    cal = FakeCal()
    r = go(conn, cal, ev("a"), ev("b"))
    assert (r.created, len(cal.events)) == (2, 2)

    r = go(conn, cal, ev("a"), ev("b"))
    assert (r.created, r.updated, r.unchanged) == (0, 0, 2)

    r = go(conn, cal, ev("a", start="2026-10-13T14:00:00+00:00", end="2026-10-13T15:30:00+00:00"), ev("b"))
    assert r.updated == 1 and cal.events["g1"]["start"]["dateTime"].startswith("2026-10-13T14")

    r = go(conn, cal, ev("a", start="2026-10-13T14:00:00+00:00", end="2026-10-13T15:30:00+00:00"))
    assert r.deleted == 1 and set(cal.events) == {"g1"}


def test_only_own_marker_and_no_past(conn):
    cal = FakeCal()
    r = go(conn, cal, ev("old", start="2026-03-01T08:00:00+00:00", end="2026-03-01T09:00:00+00:00"), ev("n"))
    assert r.created == 1
    body = next(iter(cal.events.values()))
    assert body["extendedProperties"]["private"]["planner_mirror"] == "1"
