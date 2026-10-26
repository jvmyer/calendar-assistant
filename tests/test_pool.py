from datetime import datetime, timedelta

import pytest

from planner import pool, tasks
from planner.db import connect
from planner.rules import Rules
from planner.service import planning_step
from test_planning import NOW, FakeCal, at

V = pool.LIST_TITLE
CREATED = at(28, 16, 30)   # dort legt die Google-Oberfläche neue Aufgaben ab (beliebig)


class FakeTasks:
    """Google Tasks mit mehreren Listen; Listen-ID = Titel."""

    def __init__(self):
        self.lists: dict[str, dict[str, dict]] = {V: {}}
        self.n = 0

    @property
    def items(self) -> dict[str, dict]:
        return self.lists[V]

    def ensure_list(self, title, meta_key):
        self.lists.setdefault(title, {})
        return title

    def all_lists(self):
        return [{"id": k, "title": k} for k in self.lists]

    def create_list(self, title):
        self.lists[title] = {}
        return title

    def list_tasks(self, list_id):
        return [dict(v) for v in self.lists[list_id].values()]

    def patch(self, list_id, task_id, body):
        self.lists[list_id][task_id].update(body)
        return dict(self.lists[list_id][task_id])

    def delete(self, list_id, task_id):
        self.lists.get(list_id, {}).pop(task_id, None)

    def move(self, list_id, task_id, dest):
        self.lists[dest][task_id] = self.lists[list_id].pop(task_id)


def seed(cal, tc, n, *, list_id=V, start=CREATED):
    """Wie per Chrome angelegt: Task mit Link + focusTime-Eintrag im Hauptkalender, der darauf verweist."""
    for _ in range(n):
        tc.n += 1
        key = f"k{tc.n}"
        tc.lists.setdefault(list_id, {})[f"t{tc.n}"] = {
            "id": f"t{tc.n}", "title": "Vorrat", "status": "needsAction",
            "webViewLink": f"https://tasks.google.com/task/{key}?sa=6"}
        cal.events["primary"][f"f{tc.n}"] = {
            "id": f"f{tc.n}", "eventType": "focusTime", "transparency": "transparent", "summary": "Vorrat",
            "description": f"… rufe bitte die folgende Seite auf: https://tasks.google.com/task/{key}.",
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()}}


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
    for d in range(12, 20):
        cal.busy(at(d, 9), at(d, 19))


def slot_of(cal, tc, title):
    """(Start, Ende, Task) der Vorrat-Aufgabe mit diesem Titel."""
    [t] = [t for t in tc.items.values() if t["title"] == title]
    key = t["webViewLink"].split("/task/")[1].split("?")[0]
    [ev] = [e for e in cal.events["primary"].values() if f"/task/{key}." in e["description"]]
    return datetime.fromisoformat(ev["start"]["dateTime"]), datetime.fromisoformat(ev["end"]["dateTime"]), t


def parked(cal):
    return sum(datetime.fromisoformat(e["start"]["dateTime"]) == pool.PARK_START for e in cal.events["primary"].values())


def test_new_pool_entries_are_adopted_and_parked(env):
    conn, cal, tc, _ = env
    seed(cal, tc, 3)
    tasks.add_task(conn, title="Platzhalter", source="manual", actor="user", estimate_min=30)
    cal.events["planner"].clear()
    pool.sync(conn, cal, tc, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM pool").fetchone()[0] == 3 and parked(cal) == 3
    assert all(t["title"] == "Vorrat" for t in tc.items.values())


def test_block_gets_the_checkbox_in_the_same_time_slot(env):
    """Kern des Ganzen: die Aufgabe mit Zeitraum steht genau im Zeitfenster des Blockers."""
    conn, cal, tc, rules = env
    seed(cal, tc, 3)
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60, deadline="2026-10-20")
    cycle(conn, cal, tc, rules)
    [block] = cal.events["planner"].values()
    start, end, t = slot_of(cal, tc, "Bericht")
    assert start.isoformat() == block["start"]["dateTime"] and end.isoformat() == block["end"]["dateTime"]
    assert f"Aufgabe #{tid}" in t["notes"] and t["status"] == "needsAction"
    assert parked(cal) == 2


def test_idempotent_no_writes_when_nothing_changes(env):
    conn, cal, tc, rules = env
    seed(cal, tc, 3)
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    calls = []
    cal.patch_event = lambda *a: calls.append(a)
    tc.patch = lambda *a: calls.append(a)
    for _ in range(3):
        cycle(conn, cal, tc, rules)
    assert calls == []


def test_checkbox_follows_when_the_block_moves(env):
    conn, cal, tc, rules = env
    seed(cal, tc, 2)
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    [ev] = cal.events["planner"].values()
    ev["start"]["dateTime"], ev["end"]["dateTime"] = at(14, 15).isoformat(), at(14, 16).isoformat()   # Nutzer verschiebt
    cycle(conn, cal, tc, rules)
    start, end, _ = slot_of(cal, tc, "Bericht")
    assert (start, end) == (at(14, 15), at(14, 16))


def test_ticking_the_checkbox_is_only_a_reminder(env):
    """Abhaken wirkt NICHT auf die Aufgabe – und der Planer hakt es auch nicht wieder auf."""
    conn, cal, tc, rules = env
    seed(cal, tc, 2)
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    slot_of(cal, tc, "Bericht")[2]["status"] = "completed"
    cycle(conn, cal, tc, rules)
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "open"
    assert slot_of(cal, tc, "Bericht")[2]["status"] == "completed"


def test_elapsed_task_is_ticked_then_parked_a_day_later_and_reusable(env):
    conn, cal, tc, rules = env
    seed(cal, tc, 1)
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    cycle(conn, cal, tc, rules, now=at(12, 15))                       # Block vorbei → erledigt, Haken gesetzt
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "done"
    assert slot_of(cal, tc, "Bericht")[2]["status"] == "completed"
    cycle(conn, cal, tc, rules, now=at(13, 15))                       # einen Tag später → zurück in den Vorrat
    [t] = tc.items.values()
    assert t["title"] == "Vorrat" and t["status"] == "needsAction" and parked(cal) == 1

    tasks.add_task(conn, title="Nächstes", source="manual", actor="user", estimate_min=30)
    cycle(conn, cal, tc, rules, now=at(13, 15))                       # dasselbe Kästchen wird wiederverwendet
    assert slot_of(cal, tc, "Nächstes")[2]["status"] == "needsAction"


def test_reused_checkbox_ticked_by_user_is_reopened(env):
    conn, cal, tc, rules = env
    seed(cal, tc, 1)
    a = tasks.add_task(conn, title="A", source="manual", actor="user", estimate_min=30)
    cycle(conn, cal, tc, rules)
    slot_of(cal, tc, "A")[2]["status"] = "completed"                  # Nutzer hakt ab …
    tasks.set_status(conn, a, "dropped", actor="user")                # … und verwirft die Aufgabe
    cycle(conn, cal, tc, rules)
    tasks.add_task(conn, title="B", source="manual", actor="user", estimate_min=30)
    cycle(conn, cal, tc, rules)
    assert slot_of(cal, tc, "B")[2]["status"] == "needsAction"


def test_dropped_task_frees_its_checkbox(env):
    conn, cal, tc, rules = env
    seed(cal, tc, 2)
    tid = tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    tasks.set_status(conn, tid, "dropped", actor="user")
    cycle(conn, cal, tc, rules)
    assert parked(cal) == 2 and all(t["title"] == "Vorrat" for t in tc.items.values())


def test_no_all_day_task_when_there_is_no_time(env):
    """Keine Ganztags-Tasks mehr: ohne Zeitfenster nur die (tägliche) Warnung."""
    conn, cal, tc, rules = env
    seed(cal, tc, 2)
    no_time(cal)
    tasks.add_task(conn, title="Wichtig", source="manual", actor="user", estimate_min=60, deadline="2026-10-12")
    n = cycle(conn, cal, tc, rules)
    assert not cal.events["planner"] and parked(cal) == 2 and set(tc.lists) == {V}
    assert any("Wichtig" in m for m in n.msgs)


def test_unresolved_warning_repeats_daily_but_not_every_run(env):
    conn, cal, tc, rules = env
    seed(cal, tc, 10)
    cal.busy(at(12, 9), at(12, 19))
    tasks.add_task(conn, title="Wichtig", source="manual", actor="user", estimate_min=60, deadline="2026-10-12")
    n = Collect()
    cycle(conn, cal, tc, rules, n, NOW)
    cycle(conn, cal, tc, rules, n, NOW + timedelta(hours=1))
    assert sum("Wichtig" in m for m in n.msgs) == 1
    cycle(conn, cal, tc, rules, n, NOW + timedelta(days=1))
    assert sum("Wichtig" in m for m in n.msgs) == 2


def test_low_pool_warns_once_a_day_and_blocks_stay_planned(env):
    conn, cal, tc, rules = env
    seed(cal, tc, 1)
    for i in range(3):
        tasks.add_task(conn, title=f"A{i}", source="manual", actor="user", estimate_min=30)
    n = cycle(conn, cal, tc, rules)
    assert len(cal.events["planner"]) == 3                             # geplant wird trotzdem alles
    [msg] = [m for m in n.msgs if "Vorrat" in m]
    assert "noch 0 frei" in msg and "2 Blocker" in msg and "Vorrat auffüllen" in msg
    cycle(conn, cal, tc, rules, n, NOW + timedelta(hours=1))
    assert sum("Vorrat" in m for m in n.msgs) == 1

    seed(cal, tc, 12)                                                  # Claude füllt auf
    cycle(conn, cal, tc, rules, n, NOW + timedelta(hours=2))
    assert all(slot_of(cal, tc, f"A{i}") for i in range(3))
    assert conn.execute("SELECT COUNT(*) FROM pool WHERE block_id IS NULL").fetchone()[0] == 10


def test_deleted_pool_task_is_forgotten_and_block_gets_another(env):
    conn, cal, tc, rules = env
    seed(cal, tc, 2)
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    cycle(conn, cal, tc, rules)
    gid = slot_of(cal, tc, "Bericht")[2]["id"]
    del tc.items[gid]                                                  # Nutzer löscht die Aufgabe in Google
    cycle(conn, cal, tc, rules)
    assert slot_of(cal, tc, "Bericht")[2]["id"] != gid
    assert conn.execute("SELECT action FROM changelog WHERE action='pool.lost'").fetchone()


def test_missing_calendar_entry_drops_the_row_without_crashing(env):
    conn, cal, tc, rules = env
    seed(cal, tc, 2)
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    pool.sync(conn, cal, tc, now=NOW)
    cal.events["primary"].pop("f1")
    cycle(conn, cal, tc, rules)
    assert slot_of(cal, tc, "Bericht")
    assert conn.execute("SELECT COUNT(*) FROM pool").fetchone()[0] == 1


def test_legacy_all_day_tasks_are_removed_once(env):
    conn, cal, tc, rules = env
    tid = tasks.add_task(conn, title="Alt", source="manual", actor="user", estimate_min=60)
    tc.lists["Planer"] = {"g1": {"id": "g1", "title": "Alt", "status": "needsAction"}}
    conn.execute("INSERT INTO gtasks(task_id, tasklist_id, gtask_id, kind) VALUES (?, 'Planer', 'g1', 'task')", (tid,))
    conn.commit()
    cycle(conn, cal, tc, rules)
    assert tc.lists["Planer"] == {} and conn.execute("SELECT COUNT(*) FROM gtasks").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "open"


def test_collect_strays_moves_vorrat_tasks_into_the_pool_list(env):
    _, cal, tc, _ = env
    seed(cal, tc, 2, list_id="Meine Aufgaben")
    tc.lists["Meine Aufgaben"]["x"] = {"id": "x", "title": "Einkaufen", "status": "needsAction"}
    assert pool.collect_strays(tc) == 2
    assert list(tc.lists["Meine Aufgaben"]) == ["x"] and len(tc.items) == 2


def test_run_plan_alone_does_not_touch_the_pool(env):
    from planner.planning import run_plan

    conn, cal, tc, rules = env
    seed(cal, tc, 1)
    tasks.add_task(conn, title="Bericht", source="manual", actor="user", estimate_min=60)
    run_plan(conn, cal, rules, now=NOW)
    assert parked(cal) == 0 and tc.items["t1"]["title"] == "Vorrat"
