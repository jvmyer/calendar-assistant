from datetime import UTC, datetime, timedelta

import pytest

from planner.config import Settings
from planner.connectors.ical_sync import Event, SyncResult
from planner.db import connect, get_meta, set_meta
from planner.google_auth import ReauthNeeded
from planner.service import describe_changes, sync_cycle

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:x
BEGIN:VEVENT
UID:1
DTSTAMP:20261001T000000Z
SUMMARY:Vert. Systeme
DTSTART;TZID=Europe/Berlin:20261013T154000
DTEND;TZID=Europe/Berlin:20261013T171000
LOCATION:R-203
END:VEVENT
END:VCALENDAR
"""


class Collect:
    def __init__(self):
        self.msgs = []

    def send(self, text):
        self.msgs.append(text)


class FakeCal:
    def __init__(self):
        self.events = {}

    def ensure_calendar(self, *a, **k):
        return "cal"

    def insert_event(self, cal_id, body):
        self.events[str(len(self.events))] = body
        return {"id": str(len(self.events) - 1)}

    def update_event(self, *a):
        pass

    def remove_event(self, *a):
        pass


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "t.db")
    settings = Settings(data_dir=tmp_path, google_client_id="x", google_client_secret="y",
                        raumzeit_ics_url="http://example.invalid/x.ics")
    return conn, settings


def test_cycle_syncs_and_mirrors(env):
    conn, settings = env
    cal, n = FakeCal(), Collect()
    sync_cycle(conn, settings, n, now=datetime(2026, 10, 12, 12, tzinfo=UTC),
               fetcher=lambda url: ICS, calendar_factory=lambda: cal)
    assert len(cal.events) == 1 and n.msgs == []  # Erstimport meldet nichts


def test_reauth_is_reminded_daily_not_every_cycle(env):
    conn, settings = env
    n = Collect()

    def boom():
        raise ReauthNeeded("abgelaufen")

    t0 = datetime(2026, 10, 23, 20, tzinfo=UTC)
    for minutes in (0, 30, 60):
        sync_cycle(conn, settings, n, now=t0 + timedelta(minutes=minutes),
                   fetcher=lambda url: ICS, calendar_factory=boom)
    assert sum("Google-Login abgelaufen" in m for m in n.msgs) == 1
    sync_cycle(conn, settings, n, now=t0 + timedelta(days=1),
               fetcher=lambda url: ICS, calendar_factory=boom)
    assert sum("Google-Login abgelaufen" in m for m in n.msgs) == 2


def test_source_error_does_not_crash(env):
    conn, settings = env

    def bad(url):
        raise OSError("kein Netz")

    sync_cycle(conn, settings, Collect(), fetcher=bad, calendar_factory=FakeCal)
    row = conn.execute("SELECT action FROM changelog WHERE action='sync.error'").fetchone()
    assert row is not None


def test_token_age_warning_once(env):
    conn, settings = env
    n = Collect()
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    set_meta(conn, "google_login_at", (now - timedelta(days=6, hours=1)).isoformat())
    for _ in range(2):
        sync_cycle(conn, settings, n, now=now, fetcher=lambda u: ICS, calendar_factory=FakeCal)
    assert sum("läuft bald ab" in m for m in n.msgs) == 1
    assert get_meta(conn, "expiry_warned") == "1"


def test_describe_changes():
    a = Event("a", "Vert. Systeme", "2026-10-13T13:40:00+00:00", "2026-10-13T15:10:00+00:00", location="R-203")
    b = Event("b", "Vert. Systeme", "2026-10-14T13:40:00+00:00", "2026-10-14T15:10:00+00:00", location="R-204")
    assert describe_changes(SyncResult()) is None
    msg = describe_changes(SyncResult(changed=[(a, b)], removed=[a]))
    assert "🔁" in msg and "→" in msg and "Raum R-203 → R-204" in msg and "❌" in msg
