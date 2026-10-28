import sqlite3

import pytest

from planner import changelog, tasks
from planner.db import connect, get_meta, set_meta


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


def test_add_task_logs_and_dedups(conn):
    tid = tasks.add_task(conn, title="Bericht", source="telegram", actor="user", source_ref="m1")
    assert tid is not None
    assert tasks.add_task(conn, title="Bericht", source="telegram", actor="user", source_ref="m1") is None
    rows = changelog.recent(conn)
    assert [r["action"] for r in rows] == ["task.created"]
    assert len(tasks.open_tasks(conn)) == 1


def test_status_change_is_logged(conn):
    tid = tasks.add_task(conn, title="x", source="manual", actor="user")
    tasks.set_status(conn, tid, "done", actor="user")
    assert tasks.open_tasks(conn) == []
    assert changelog.recent(conn, 1)[0]["action"] == "task.status"
    with pytest.raises(ValueError):
        tasks.set_status(conn, tid, "kaputt", actor="user")


def test_changelog_is_append_only(conn):
    changelog.log(conn, actor="system", action="a", entity="e")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE changelog SET actor = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM changelog")


def test_meta_roundtrip(conn):
    assert get_meta(conn, "k") is None
    set_meta(conn, "k", "v1")
    set_meta(conn, "k", "v2")
    assert get_meta(conn, "k") == "v2"


def test_open_hka_and_ilias_tasks_get_uni_area_once(tmp_path):
    from planner import areas, tasks
    from planner.db import connect

    conn = connect(tmp_path / "t.db")
    conn.execute("INSERT INTO mails(item_id, received, first_seen, state, source) VALUES ('h', 'x', 'x', 'done', 'hka')")
    conn.execute("INSERT INTO mails(item_id, received, first_seen, state, source) VALUES ('g', 'x', 'x', 'done', 'gmail')")
    for title, source, ref in [("A", "mail", "h"), ("B", "mail", "g"), ("C", "ilias", "n1"), ("D", "telegram", None)]:
        tasks.add_task(conn, title=title, source=source, source_ref=ref, actor="test")
    conn.execute("DELETE FROM meta WHERE key = 'migrated:uni_area'")   # wie eine alte Datenbank
    conn.commit()
    conn = connect(tmp_path / "t.db")
    got = {r["title"]: areas.path_of(conn, r["area_id"]) for r in conn.execute("SELECT * FROM tasks")}
    assert got == {"A": "Uni", "B": "", "C": "Uni", "D": ""}
