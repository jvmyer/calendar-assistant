from datetime import datetime, timedelta

import pytest

from planner import tasks
from planner.db import connect
from planner.planning import run_plan
from planner.rules import Rules
from planner.service import planning_step
from test_planning import NOW, FakeCal, at


class FakeTasks:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.n = 0

    def ensure_list(self, *a):
        return "L"

    def list_tasks(self, list_id):
        return [dict(v) for v in self.items.values()]

    def insert(self, list_id, body):
        self.n += 1
        t = {"id": f"t{self.n}", "status": "needsAction", **body}
        self.items[t["id"]] = t
        return dict(t)

    def patch(self, list_id, task_id, body):
        self.items[task_id].update(body)
        return dict(self.items[task_id])

    def delete(self, list_id, task_id):
        self.items.pop(task_id, None)

    def only(self):
        assert len(self.items) == 1
        return next(iter(self.items.values()))


class Collect:
    def __init__(self):
        self.msgs = []

    def send(self, text):
        self.msgs.append(text)


@pytest.fixture
def env(tmp_path):
    return connect(tmp_path / "t.db"), FakeCal(), FakeTasks(), Rules(horizon_days=7)


def cycle(conn, cal, tc, rules, notifier=None, now=NOW):
    notifier = notifier or Collect()
    planning_step(conn, cal, tc, rules, notifier, now)
    return notifier


def test_planned_task_has_google_task_with_block_info(env):
    conn, cal, tc, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60, deadline="2026-10-20")
    cycle(conn, cal, tc, rules)
    t = tc.only()
    assert t["title"] == "Bericht" and t["due"].startswith("2026-10-20")
    assert "Eingeplant" in t["notes"] and "09:00" in t["notes"]
    assert len(cal.events["planner"]) == 1   # Zeit war da → Blocker UND Task


def test_no_time_means_task_only_and_never_lost(env):
    conn, cal, tc, rules = env
    cal.busy(at(12, 9), at(12, 19))
    tasks.add_task(conn, title="Wichtig", source="manual", actor="user", estimate_min=60, deadline="2026-10-12")
    n = cycle(conn, cal, tc, rules)
    assert not cal.events["planner"]                       # kein Blocker …
    t = tc.only()                                           # … aber die Task steht da
    assert t["due"].startswith("2026-10-12") and "ohne freien Slot" in t["notes"]
    assert any("Wichtig" in m and "Task" in m for m in n.msgs)   # und der Nutzer wird gewarnt


def test_unresolved_warning_repeats_daily_but_not_every_run(env):
    conn, cal, tc, rules = env
    cal.busy(at(12, 9), at(12, 19))
    tasks.add_task(conn, title="Wichtig", source="manual", actor="user", estimate_min=60, deadline="2026-10-12")
    n = Collect()
    cycle(conn, cal, tc, rules, n, NOW)
    cycle(conn, cal, tc, rules, n, NOW + timedelta(hours=1))
    assert sum("Wichtig" in m for m in n.msgs) == 1
    cycle(conn, cal, tc, rules, n, NOW + timedelta(days=1))   # nächster Tag, immer noch ungelöst
    assert sum("Wichtig" in m for m in n.msgs) == 2


def test_task_without_deadline_or_slot_still_gets_a_date(env):
    conn, cal, tc, rules = env
    for d in range(12, 20):   # alles im Horizont belegt
        cal.busy(at(d, 9), at(d, 19))
    tasks.add_task(conn, title="Irgendwann", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    assert tc.only()["due"].startswith("2026-10-12")   # nie ohne Datum → im Kalender sichtbar


def test_idempotent_no_duplicate_tasks(env):
    conn, cal, tc, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    snapshot = {k: dict(v) for k, v in tc.items.items()}
    for _ in range(3):
        cycle(conn, cal, tc, rules)
    assert tc.items == snapshot


def test_block_move_updates_task_notes(env):
    conn, cal, tc, rules = env
    cal.busy(at(12, 9), at(12, 19))
    cal.busy(at(13, 9), at(13, 19))
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    assert "Fr 16.10." not in tc.only()["notes"] and "14.10." in tc.only()["notes"]
    cal.busy(at(14, 9), at(14, 19))
    cycle(conn, cal, tc, rules)
    assert "15.10." in tc.only()["notes"]


def test_user_checks_off_google_task(env):
    conn, cal, tc, rules = env
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    tc.only()["status"] = "completed"
    cycle(conn, cal, tc, rules)
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "done"
    assert not cal.events["planner"]   # Blocker verschwindet mit der erledigten Aufgabe
    assert conn.execute("SELECT action FROM changelog WHERE action='task.status'").fetchone() is not None


def test_user_deletes_google_task(env):
    conn, cal, tc, rules = env
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    tc.items.clear()
    cycle(conn, cal, tc, rules)
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "dropped"
    assert not tc.items   # wird nicht wieder angelegt


def test_deleted_task_list_is_recreated_not_treated_as_dropped(env):
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    conn, cal, tc, rules = env
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)

    class ListGoneOnce(FakeTasks):
        gone = True

        def ensure_list(self, title, meta_key):
            return "L2" if not self.gone else "L"

        def list_tasks(self, list_id):
            if self.gone and list_id == "L":
                self.gone = False
                self.items.clear()
                raise HttpError(Response({"status": 404}), b"")
            return super().list_tasks(list_id)

    tc2 = ListGoneOnce()
    tc2.items = dict(tc.items)
    cycle(conn, cal, tc2, rules)
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "open"
    assert tc2.only()["title"] == "Bericht"   # Task wurde in der neuen Liste wiederhergestellt
    assert conn.execute("SELECT action FROM changelog WHERE action='gtasks.list_recreated'").fetchone()


def test_local_done_completes_remote_and_drop_deletes_it(env):
    conn, cal, tc, rules = env
    a = tasks.add_task(conn, title="A", source="manual", actor="user", estimate_min=30)
    b = tasks.add_task(conn, title="B", source="manual", actor="user", estimate_min=30)
    cycle(conn, cal, tc, rules)
    tasks.set_status(conn, a, "done", actor="user")
    tasks.set_status(conn, b, "dropped", actor="user")
    cycle(conn, cal, tc, rules)
    assert [t["title"] for t in tc.items.values()] == ["A"] and tc.only()["status"] == "completed"


def test_past_blocks_note(env):
    conn, cal, tc, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    cycle(conn, cal, tc, rules, now=datetime(2026, 10, 12, 15, 0, tzinfo=NOW.tzinfo))
    assert "vorbei" in tc.only()["notes"]


def test_run_plan_alone_does_not_touch_tasks(env):
    conn, cal, tc, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    assert not tc.items
