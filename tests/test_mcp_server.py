import pytest
from starlette.testclient import TestClient

from planner import mcp_server, proposals, tasks
from planner.config import Settings
from planner.db import connect


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, google_client_id="x", google_client_secret="x",
                        mcp_token="secret")
    monkeypatch.setattr(mcp_server, "load_settings", lambda: settings)
    return settings.db_path


def conn_for(db_path):
    return connect(db_path)


def test_add_task_creates_real_task(env):
    out = mcp_server.add_task("Bericht schreiben", deadline="20.10.2026", area="Uni/Thesis", estimate="90")
    assert "task_id" in out
    conn = conn_for(env)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (out["task_id"],)).fetchone()
    assert row["title"] == "Bericht schreiben" and row["source"] == "claude" and row["estimate_min"] == 90


def test_add_task_invalid_deadline_is_reported_not_raised(env):
    out = mcp_server.add_task("X", deadline="nicht lesbar")
    assert "error" in out


def test_propose_event_needs_time(env):
    out = mcp_server.propose("Ohne Zeit", kind="event", start="5.11.", end="5.11.")
    assert "error" in out


def test_propose_then_decide_accept_creates_task(env):
    out = mcp_server.propose("Vielleicht", kind="task", deadline="20.10.2026")
    assert "proposal_id" in out
    decided = mcp_server.decide_proposal(out["proposal_id"], True)
    assert decided["accepted"] is True and decided["task_id"] is not None


def test_decide_unknown_proposal_reports_error(env):
    out = mcp_server.decide_proposal(999, True)
    assert "error" in out


def test_set_task_status(env):
    added = mcp_server.add_task("Erledigen")
    out = mcp_server.set_task_status(added["task_id"], "done")
    assert out["status"] == "done"
    conn = conn_for(env)
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (added["task_id"],)).fetchone()[0] == "done"


def test_list_open_tasks_hides_mail_and_voice_sources(env):
    mcp_server.add_task("Sichtbar")
    conn = conn_for(env)
    tasks.add_task(conn, title="Aus einer Mail", source="mail", actor="system", source_ref="m1")
    tasks.add_task(conn, title="Aus Sprachnachricht", source="voice", actor="system", source_ref="v1")
    titles = {t["title"] for t in mcp_server.list_open_tasks()}
    assert titles == {"Sichtbar"}


def test_list_pending_proposals_hides_mail_sources(env):
    mcp_server.propose("Sichtbar", kind="task", deadline="20.10.2026")
    conn = conn_for(env)
    proposals.add_proposal(conn, title="Aus einer Mail", kind="task", source="mail", actor="system",
                           source_ref="m1", deadline="2026-10-20")
    titles = {p["title"] for p in mcp_server.list_pending_proposals()}
    assert titles == {"Sichtbar"}


def test_area_tree_reflects_created_areas(env):
    mcp_server.add_task("X", area="Uni/Modul-X")
    tree = mcp_server.area_tree()
    assert [n["name"] for n in tree] == ["Uni", "Modul-X"]


def test_build_app_refuses_without_token(tmp_path):
    settings = Settings(data_dir=tmp_path, google_client_id="x", google_client_secret="x", mcp_token="")
    with pytest.raises(ValueError):
        mcp_server.build_app(settings)


def test_http_app_rejects_missing_or_wrong_bearer_token(tmp_path):
    settings = Settings(data_dir=tmp_path, google_client_id="x", google_client_secret="x", mcp_token="secret")
    app = mcp_server.build_app(settings)
    with TestClient(app, base_url=f"http://{settings.mcp_host}:{settings.mcp_port}") as client:
        assert client.post("/mcp", json={}).status_code == 401
        assert client.post("/mcp", json={}, headers={"authorization": "Bearer falsch"}).status_code == 401
        # Richtiges Token kommt durch die eigene Prüfung – der Rest ist MCP-Protokoll (SDK, nicht hier getestet).
        r = client.post("/mcp", json={}, headers={"authorization": "Bearer secret"})
        assert r.status_code != 401
