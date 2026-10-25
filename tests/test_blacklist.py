import pytest

from planner import blacklist, changelog, tasks
from planner.db import connect


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


def test_normalize_trims_case_and_whitespace():
    assert blacklist.normalize("  Bibliotheksgebühr   zahlen  ") == "bibliotheksgebühr zahlen"


def test_add_is_idempotent_and_logged(conn):
    bid = blacklist.add(conn, "Sportkurs Anmeldung", actor="user")
    assert bid is not None
    assert blacklist.add(conn, "sportkurs   anmeldung", actor="user") is None   # gleicher normalisierter Titel
    assert len(blacklist.list_all(conn)) == 1
    assert changelog.recent(conn, 1)[0]["action"] == "blacklist.added"


def test_add_empty_title_is_noop(conn):
    assert blacklist.add(conn, "   ", actor="user") is None
    assert blacklist.list_all(conn) == []


def test_remove_unknown_raises(conn):
    with pytest.raises(KeyError):
        blacklist.remove(conn, 999, actor="user")


def test_remove_logs_and_unblocks_future_creation(conn):
    bid = blacklist.add(conn, "Sportkurs Anmeldung", actor="user")
    assert blacklist.remove(conn, bid, actor="user") == "Sportkurs Anmeldung"
    assert changelog.recent(conn, 1)[0]["action"] == "blacklist.removed"
    assert tasks.add_task(conn, title="Sportkurs Anmeldung", source="manual", actor="user") is not None


def test_add_task_is_blocked_by_blacklist(conn):
    blacklist.add(conn, "Sportkurs Anmeldung", actor="user")
    assert tasks.add_task(conn, title="Sportkurs Anmeldung", source="manual", actor="user") is None
    assert tasks.open_tasks(conn) == []
    # Nur Mail-Dedup-Einträge im Log, kein task.created
    assert "task.created" not in [r["action"] for r in changelog.recent(conn, 5)]


def test_add_task_blacklist_match_is_case_and_whitespace_insensitive(conn):
    blacklist.add(conn, "sportkurs anmeldung", actor="user")
    assert tasks.add_task(conn, title="  Sportkurs   Anmeldung ", source="manual", actor="user") is None
