from datetime import UTC, datetime

import pytest

from planner import changelog
from planner.connectors.ical_sync import parse_ics, sync
from planner.db import connect

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def ics(*events: str) -> str:
    return "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\n" + "\n".join(events) + "\nEND:VCALENDAR\n"


def ev(uid, start, end, title="Vorlesung", loc="R-201", extra=""):
    return (
        f"BEGIN:VEVENT\nUID:{uid}\nDTSTAMP:20261001T000000Z\nSUMMARY:{title if title != 'Vorlesung' else 'V ' + uid}\n"
        f"DTSTART;TZID=Europe/Berlin:{start}\nDTEND;TZID=Europe/Berlin:{end}\n"
        f"LOCATION:{loc}\n{extra}END:VEVENT"
    )


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


def run(conn, *events):
    return sync(conn, "raumzeit", parse_ics(ics(*events)), now=NOW)


def test_parse_converts_to_utc():
    (e,) = parse_ics(ics(ev("a", "20261013T140000", "20261013T153000")))
    assert e.start == "2026-10-13T12:00:00+00:00"  # CEST = UTC+2
    assert e.location == "R-201"


def test_first_sync_is_silent_import(conn):
    res = run(conn, ev("a", "20261013T140000", "20261013T153000"))
    assert res.initial and not res.added
    assert [r["action"] for r in changelog.recent(conn)] == ["source.initial_import"]


def test_detects_move_room_change_and_removal(conn):
    a = ev("a", "20261013T140000", "20261013T153000")
    b = ev("b", "20261014T100000", "20261014T113000")
    run(conn, a, b)

    res = run(conn, ev("a", "20261013T154000", "20261013T171000"), b)  # a verschoben
    assert len(res.changed) == 1 and res.changed[0][0].title == "V a" and not res.removed
    assert not (res.added or res.removed)

    res = run(conn, ev("a", "20261013T154000", "20261013T171000", loc="E-999"), b)  # Raum
    assert len(res.changed) == 1

    res = run(conn, ev("a", "20261013T154000", "20261013T171000", loc="E-999"))  # b entfällt
    assert [e.title for e in res.removed] == ["V b"]

    res = run(conn, ev("a", "20261013T154000", "20261013T171000", loc="E-999"))  # stabil
    assert not (res.added or res.changed or res.removed)


def test_new_event_after_initial_is_added(conn):
    run(conn, ev("a", "20261013T140000", "20261013T153000"))
    res = run(conn, ev("a", "20261013T140000", "20261013T153000"),
              ev("c", "20261015T080000", "20261015T093000"))
    assert [e.title for e in res.added] == ["V c"]


def test_past_events_dropping_out_are_ignored(conn):
    run(conn, ev("old", "20260301T080000", "20260301T093000"))
    res = run(conn)  # Feed ohne den alten Termin
    assert not res.removed


def test_cancelled_events_are_skipped():
    assert parse_ics(ics(ev("x", "20261013T140000", "20261013T153000", extra="STATUS:CANCELLED\n"))) == []


def test_renumbered_uids_cause_no_changes(conn):
    """Raumzeit nummeriert UIDs durch (44532_159 -> 44532_162): darf keine Änderung auslösen."""
    run(conn, ev("44532_159@x", "20261013T140000", "20261013T153000", title="SCKM"),
        ev("44464_160@x", "20261014T154000", "20261014T171000", title="IME"))
    res = run(conn, ev("44532_162@x", "20261013T140000", "20261013T153000", title="SCKM"),
              ev("44464_163@x", "20261014T154000", "20261014T171000", title="IME"))
    assert not (res.added or res.changed or res.removed)


def test_moved_event_is_paired_not_added_and_removed(conn):
    run(conn, ev("1", "20261117T154000", "20261117T171000", title="IME"))
    res = run(conn, ev("2", "20261110T154000", "20261110T171000", title="IME"))
    assert len(res.changed) == 1 and not res.added and not res.removed
    assert res.changed[0][0].start != res.changed[0][1].start
