from datetime import datetime

import pytest

from planner import tasks
from planner.db import connect
from planner.planning import describe_plan, run_plan
from planner.rules import TZ, Rules

NOW = datetime(2026, 10, 12, 8, 0, tzinfo=TZ)   # Montag 08:00


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, tzinfo=TZ)


class FakeCal:
    def __init__(self):
        self.calendars = [{"id": "planner", "summary": "Planer"}, {"id": "main", "summary": "Uni"},
                          {"id": "de#holiday@group.v.calendar.google.com", "summary": "Feiertage"}]
        self.events: dict[str, dict] = {"planner": {}, "main": {}, "de#holiday@group.v.calendar.google.com": {}}
        self.n = 0

    def ensure_planner_calendar(self):
        return "planner"

    def list_calendars(self):
        return self.calendars

    def list_events(self, cid, start, end):
        return list(self.events[cid].values())

    def _put(self, eid, title, start, end):
        self.events["planner"][eid] = {"id": eid, "summary": title,
                                       "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}

    def create_block(self, title, start, end, *, task_id, description=""):
        self.n += 1
        self._put(f"e{self.n}", title, start, end)
        return {"id": f"e{self.n}"}

    def update_block(self, event_id, title, start, end, *, task_id, description=""):
        self._put(event_id, title, start, end)

    def delete_block(self, event_id):
        self.events["planner"].pop(event_id, None)

    def busy(self, start, end, cid="main", **extra):
        self.n += 1
        self.events[cid][f"b{self.n}"] = {"id": f"b{self.n}", "start": {"dateTime": start.isoformat()},
                                          "end": {"dateTime": end.isoformat()}, **extra}


@pytest.fixture
def env(tmp_path):
    return connect(tmp_path / "t.db"), FakeCal(), Rules(horizon_days=7)


def starts(cal):
    return sorted(datetime.fromisoformat(e["start"]["dateTime"]) for e in cal.events["planner"].values())


def test_creates_block_and_is_idempotent(env):
    conn, cal, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    r1 = run_plan(conn, cal, rules, now=NOW)
    assert [c.kind for c in r1.changes] == ["created"]
    assert starts(cal) == [at(12, 9)]
    r2 = run_plan(conn, cal, rules, now=NOW)
    assert not r2.changed
    assert conn.execute("SELECT COUNT(*) FROM changelog WHERE action='block.created'").fetchone()[0] == 1


def test_dry_run_writes_nothing(env):
    conn, cal, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    report = run_plan(conn, cal, rules, now=NOW, dry_run=True)
    assert report.changed and not cal.events["planner"]
    assert conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0


def test_ignores_holiday_allday_and_free_events(env):
    conn, cal, rules = env
    cal.busy(at(12, 9), at(12, 19), cid="de#holiday@group.v.calendar.google.com")
    cal.events["main"]["x"] = {"id": "x", "start": {"date": "2026-10-12"}, "end": {"date": "2026-10-13"}}
    cal.busy(at(12, 9), at(12, 19), transparency="transparent")
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    assert starts(cal) == [at(12, 9)]


def test_respects_busy_with_buffer(env):
    conn, cal, rules = env
    cal.busy(at(12, 9), at(12, 10))
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    assert starts(cal) == [at(12, 10, 15)]   # 10:00 + 10 Min. Puffer, aufs 15er-Raster


def test_outside_freeze_block_moves_on_conflict(env):
    conn, cal, rules = env
    cal.busy(at(12, 9), at(12, 19))   # Mo und Di komplett belegt
    cal.busy(at(13, 9), at(13, 19))
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    assert starts(cal) == [at(14, 9)]
    cal.busy(at(14, 9), at(14, 10))
    report = run_plan(conn, cal, rules, now=NOW)
    assert [c.kind for c in report.changes] == ["moved"]
    assert starts(cal) == [at(14, 10, 15)]
    assert len(cal.events["planner"]) == 1


def test_freeze_keeps_stable_blocks_without_conflict(env):
    conn, cal, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    # eine wichtigere Aufgabe kommt dazu; der bestehende Blocker heute bleibt trotzdem stehen
    tasks.add_task(conn, title="Dringend", source="manual", actor="user", estimate_min=60,
                   deadline="2026-10-12")
    run_plan(conn, cal, rules, now=NOW)
    assert at(12, 9) in starts(cal)
    titles = {e["summary"]: e["start"]["dateTime"] for e in cal.events["planner"].values()}
    assert titles["Bericht"].startswith("2026-10-12T09:00")


def test_freeze_releases_block_on_conflict(env):
    conn, cal, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    cal.busy(at(12, 9), at(12, 10))   # Meeting kommt rein
    report = run_plan(conn, cal, rules, now=NOW)
    assert [c.kind for c in report.changes] == ["moved"]
    assert starts(cal) == [at(12, 10, 15)]


def test_user_move_pins_block(env):
    conn, cal, rules = env
    cal.busy(at(12, 9), at(12, 19))
    cal.busy(at(13, 9), at(13, 19))
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    eid = next(iter(cal.events["planner"]))
    cal._put(eid, "Bericht", at(16, 15), at(16, 16))   # Nutzer zieht den Blocker auf Freitag
    cal.busy(at(16, 15), at(16, 17))                    # sogar auf einen Konflikt
    report = run_plan(conn, cal, rules, now=NOW)
    assert report.pinned == 1 and not report.changed
    assert starts(cal) == [at(16, 15)]
    assert conn.execute("SELECT pinned FROM blocks").fetchone()[0] == 1


def test_user_delete_is_respected(env):
    conn, cal, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    cal.events["planner"].clear()   # Nutzer löscht den Blocker
    report = run_plan(conn, cal, rules, now=NOW)
    assert report.removed_by_user == 1
    assert starts(cal) == [at(12, 10)]   # nicht wieder an derselben Stelle
    assert conn.execute("SELECT COUNT(*) FROM blocks WHERE state='removed'").fetchone()[0] == 1


def test_done_task_removes_blocks(env):
    conn, cal, rules = env
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    tasks.set_status(conn, tid, "done", actor="user")
    report = run_plan(conn, cal, rules, now=NOW)
    assert [c.kind for c in report.changes] == ["deleted"]
    assert not cal.events["planner"]


def test_past_blocks_count_as_worked(env):
    conn, cal, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=120)
    run_plan(conn, cal, rules, now=NOW)   # 09:00–11:00
    later = datetime(2026, 10, 12, 12, 0, tzinfo=TZ)
    report = run_plan(conn, cal, rules, now=later)
    assert not report.changed   # nichts mehr zu tun, wartet auf „erledigt“


def test_warning_only_reported_once(env):
    conn, cal, rules = env
    cal.busy(at(12, 9), at(12, 19))
    tasks.add_task(conn, title="Knapp", source="manual", actor="user", estimate_min=120, deadline="2026-10-12")
    first = run_plan(conn, cal, rules, now=NOW)
    second = run_plan(conn, cal, rules, now=NOW)
    assert len(first.warnings) == 1 and "Knapp" in first.warnings[0]
    assert second.warnings == []


def test_area_weight_influences_order(env):
    from planner import areas

    conn, cal, rules = env
    work = areas.ensure_path(conn, "Arbeit", actor="user")
    priv = areas.ensure_path(conn, "Privat", actor="user")
    areas.set_weight(conn, priv, 0.3, actor="user")
    tasks.add_task(conn, title="Privates", source="manual", actor="user", estimate_min=60, area_id=priv)
    tasks.add_task(conn, title="Job", source="manual", actor="user", estimate_min=60, area_id=work)
    run_plan(conn, cal, rules, now=NOW)
    by_start = sorted(cal.events["planner"].values(), key=lambda e: e["start"]["dateTime"])
    assert by_start[0]["summary"] == "Job"


def test_describe_plan(env):
    conn, cal, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    msg = describe_plan(run_plan(conn, cal, rules, now=NOW))
    assert msg and "➕" in msg and "Bericht" in msg
    assert describe_plan(run_plan(conn, cal, rules, now=NOW)) is None
