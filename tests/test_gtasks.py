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


def no_time(cal):
    """Alles im Planungshorizont belegt → es gibt kein Zeitfenster, also nur die Task als Auffangnetz."""
    for d in range(12, 20):
        cal.busy(at(d, 9), at(d, 19))


def test_task_with_time_slot_also_gets_a_companion_task(env):
    """Mit Zeitfenster steht der Blocker im Kalender UND eine Begleit-Task zum Abhaken daneben."""
    conn, cal, tc, rules = env
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60, deadline="2026-10-20")
    cycle(conn, cal, tc, rules)
    [ev] = cal.events["planner"].values()
    assert ev["summary"] == "Bericht" and ev["start"]["dateTime"].startswith("2026-10-12T09:00")
    google = tc.only()
    assert google["title"] == "Bericht" and google["due"].startswith("2026-10-12")   # Datum des Blocks, nicht der Deadline
    assert conn.execute("SELECT kind FROM gtasks WHERE task_id=?", (tid,)).fetchone()[0] == "companion"


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
    no_time(cal)
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    snapshot = {k: dict(v) for k, v in tc.items.items()}
    assert len(snapshot) == 1
    for _ in range(3):
        cycle(conn, cal, tc, rules)
    assert tc.items == snapshot


def test_task_switches_between_safety_net_and_companion_as_time_appears_and_vanishes(env):
    conn, cal, tc, rules = env
    no_time(cal)
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    assert tc.only()["title"] == "Bericht" and not cal.events["planner"]        # keine Zeit → nur Auffangnetz-Task
    assert conn.execute("SELECT kind FROM gtasks WHERE task_id=?", (tid,)).fetchone()[0] == "task"

    cal.events["main"].clear()                                                  # Termine fallen weg → Zeit ist da
    cycle(conn, cal, tc, rules)
    assert len(cal.events["planner"]) == 1                                      # Blocker kommt dazu …
    assert tc.only()["title"] == "Bericht"                                      # … die Task bleibt, wird zum Begleiter
    assert conn.execute("SELECT kind FROM gtasks WHERE task_id=?", (tid,)).fetchone()[0] == "companion"

    cal.events["planner"].clear()                                               # Nutzer löscht den Blocker …
    no_time(cal)                                                                # … und es bleibt keine Zeit
    cycle(conn, cal, tc, rules)
    assert not tc.items                                                         # kein Auffangnetz – Löschen gilt
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "dropped"


def test_user_checks_off_google_task(env):
    conn, cal, tc, rules = env
    no_time(cal)
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    tc.only()["status"] = "completed"
    cycle(conn, cal, tc, rules)
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "done"
    assert conn.execute("SELECT action FROM changelog WHERE action='task.status'").fetchone() is not None


def test_user_deletes_google_task(env):
    conn, cal, tc, rules = env
    no_time(cal)
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
    no_time(cal)
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
    no_time(cal)
    a = tasks.add_task(conn, title="A", source="manual", actor="user", estimate_min=30)
    b = tasks.add_task(conn, title="B", source="manual", actor="user", estimate_min=30)
    cycle(conn, cal, tc, rules)
    assert len(tc.items) == 2
    tasks.set_status(conn, a, "done", actor="user")
    tasks.set_status(conn, b, "dropped", actor="user")
    cycle(conn, cal, tc, rules)
    assert [t["title"] for t in tc.items.values()] == ["A"] and tc.only()["status"] == "completed"


def test_elapsed_block_closes_the_task_and_completes_the_companion(env):
    """Ohne Abhaken: nach dem Block ist die Aufgabe erledigt – die Begleit-Task wird mit abgeschlossen."""
    conn, cal, tc, rules = env
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    cycle(conn, cal, tc, rules, now=datetime(2026, 10, 12, 15, 0, tzinfo=NOW.tzinfo))
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "done"
    assert tc.only()["status"] == "completed"


def test_checking_off_a_companion_task_does_not_complete_the_underlying_task(env):
    """Begleit-Task ist nur eine Erinnerung – Abhaken/Löschen wirkt NICHT auf die Aufgabe zurück."""
    conn, cal, tc, rules = env
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    assert conn.execute("SELECT kind FROM gtasks WHERE task_id=?", (tid,)).fetchone()[0] == "companion"
    tc.only()["status"] = "completed"
    cycle(conn, cal, tc, rules)
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "open"

    tc.items.clear()                                                            # Begleit-Task gelöscht
    cycle(conn, cal, tc, rules)
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "open"
    assert tc.items                                                             # wird einfach neu angelegt


def test_partly_planned_task_keeps_a_google_task_for_the_unplaced_rest(env):
    conn, cal, tc, rules = env
    cal.busy(at(12, 10), at(12, 19))                      # heute nur 09:00–09:45 frei, danach alles belegt
    for d in range(13, 20):
        cal.busy(at(d, 9), at(d, 19))
    tasks.add_task(conn, title="Groß", source="manual", actor="user", estimate_min=120)
    cycle(conn, cal, tc, rules)
    cycle(conn, cal, tc, rules, now=datetime(2026, 10, 12, 15, 0, tzinfo=NOW.tzinfo))   # der kleine Block ist vorbei
    assert tc.only()["title"] == "Groß" and "75 Min. ohne freien Slot" in tc.only()["notes"]   # Rest geht nicht unter


def test_run_plan_alone_does_not_touch_tasks(env):
    conn, cal, tc, rules = env
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    assert not tc.items
