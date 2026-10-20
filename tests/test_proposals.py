from datetime import datetime

import pytest

from planner import proposals, tasks
from planner.db import connect
from planner.rules import TZ, Rules
from test_gtasks import FakeTasks, cycle
from test_planning import FakeCal, at


class CalWithInsert(FakeCal):
    def insert_event(self, cal_id, body):
        self.n += 1
        ev = {"id": f"p{self.n}", **body}
        self.events[cal_id][ev["id"]] = ev
        return {"id": ev["id"]}


@pytest.fixture
def env(tmp_path):
    return connect(tmp_path / "t.db"), CalWithInsert(), FakeTasks(), Rules(horizon_days=7)


def barcamp(conn):
    return proposals.add_proposal(
        conn, title="KI-Barcamp", kind="event", source="mail", actor="system", source_ref="m1",
        start=datetime(2026, 11, 5, 13, 30, tzinfo=TZ).isoformat(), end=datetime(2026, 11, 5, 17, 30, tzinfo=TZ).isoformat())


def planner_events(cal):
    return list(cal.events["planner"].values())


def test_pending_proposal_never_reaches_calendar_or_tasks(env):
    conn, cal, tc, rules = env
    barcamp(conn)
    proposals.add_proposal(conn, title="Waffeln melden", kind="task", source="mail", actor="system",
                           source_ref="m2", deadline="2026-10-19")
    for _ in range(3):
        cycle(conn, cal, tc, rules)
    assert not planner_events(cal) and not tc.items
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_pending_proposal_is_never_pushed_only_pullable(env):
    """Vorschläge stören nicht mehr per Telegram – nur `/vorschlaege` zeigt sie."""
    conn, cal, tc, rules = env
    barcamp(conn)
    notifier = cycle(conn, cal, tc, rules)
    cycle(conn, cal, tc, rules, notifier)
    assert not any("Vorschlag" in m for m in notifier.msgs)
    assert [r["title"] for r in proposals.pending(conn)] == ["KI-Barcamp"]


def test_dedupe_by_source_ref(env):
    conn, *_ = env
    assert barcamp(conn) is not None and barcamp(conn) is None


def test_accepted_event_lands_in_calendar_once(env):
    conn, cal, tc, rules = env
    pid = barcamp(conn)
    cycle(conn, cal, tc, rules)
    assert proposals.accept(conn, pid, actor="user") is None
    cycle(conn, cal, tc, rules)
    cycle(conn, cal, tc, rules)
    evs = planner_events(cal)
    assert len(evs) == 1 and evs[0]["summary"] == "KI-Barcamp"


def test_accepted_event_blocks_planning_time(env):
    conn, cal, tc, rules = env
    pid = proposals.add_proposal(
        conn, title="Termin", kind="event", source="manual", actor="user",
        start=at(12, 9).isoformat(), end=at(12, 11).isoformat())
    proposals.accept(conn, pid, actor="user")
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    blocks = [e for e in planner_events(cal) if e["summary"] == "Bericht"]
    assert datetime.fromisoformat(blocks[0]["start"]["dateTime"]) >= at(12, 11)   # nicht in den Termin hinein


def test_accepted_task_becomes_real_task_and_gets_a_block(env):
    conn, cal, tc, rules = env
    pid = proposals.add_proposal(conn, title="Anmeldung", kind="task", source="mail", actor="system",
                                 source_ref="m3", deadline="2026-10-23", estimate_min=10)
    task_id = proposals.accept(conn, pid, actor="user")
    assert task_id is not None
    cycle(conn, cal, tc, rules)
    assert [e["summary"] for e in planner_events(cal)] == ["Anmeldung"]      # Zeit war da → Blocker
    assert tc.only()["title"] == "Anmeldung"                                     # und eine Begleit-Task zum Abhaken


def test_rejected_never_materializes(env):
    conn, cal, tc, rules = env
    pid = barcamp(conn)
    proposals.reject(conn, pid, actor="user")
    cycle(conn, cal, tc, rules)
    assert not planner_events(cal)
    with pytest.raises(ValueError):
        proposals.accept(conn, pid, actor="user")   # einmal entschieden, endgültig


def test_event_needs_times(env):
    conn, *_ = env
    with pytest.raises(ValueError):
        proposals.add_proposal(conn, title="x", kind="event", source="manual", actor="user")
