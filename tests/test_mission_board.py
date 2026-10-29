"""Mission Board (schema v6): column model, stories, worker models, agent
proposals and the sessions registry — store, MCP and REST."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from kanban_mcp import server
from kanban_mcp.policy import check_agent_may_use
from kanban_store import STATUSES, Store, status_meta
from kanban_store.store import normalize_status
from kanban_ui import main
from kanban_ui.automation import rules as rules_mod


# ---------------------------------------------------------------- store


def test_column_model():
    assert STATUSES == ["backlog", "analysis", "approved", "in_progress", "testing",
                        "results", "done", "blocked", "cancelled"]
    hidden = [c["id"] for c in status_meta() if c.get("hidden")]
    assert hidden == ["testing"]
    # pre-v6 names and friendly spellings still resolve
    assert normalize_status("uat") == "results"
    assert normalize_status("Analyst") == "analysis"
    assert normalize_status("Review") == "results"


def test_migration_from_v5_maps_old_columns(tmp_path):
    db = tmp_path / "v5.db"
    s = Store(db)
    s.create_project("p", "P")
    a = s.create_task("claimed", project_id="p", status="in_progress")
    b = s.create_task("in review", project_id="p", status="results")
    s.close()
    # rewind the file to a v5 board that used the old column ids
    conn = sqlite3.connect(db)
    conn.execute("UPDATE tasks SET status='analyst' WHERE id=?", (a.id,))
    conn.execute("UPDATE tasks SET status='uat' WHERE id=?", (b.id,))
    conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    s = Store(db)
    try:
        assert s.get_task(a.id).status == "in_progress"
        assert s.get_task(b.id).status == "results"
        assert s._schema_version() == 6
        assert s.get_project("p").default_model == "sonnet"
    finally:
        s.close()


def test_story_fields_and_parent(store):
    story = store.create_task(
        "Online gyro bias", project_id="proj", type="Story",
        story_as_a="an operator", story_i_want="the gyro bias estimated online",
        story_so_that="heading drift stays low", acceptance="- [ ] converges in replay",
    )
    assert story.type == "story" and story.story_as_a == "an operator"
    child = store.create_task("Replay UI", project_id="proj", parent_id=story.id)
    assert child.parent_id == story.id and child.type == "task"
    with pytest.raises(ValueError, match="unknown parent"):
        store.create_task("x", project_id="proj", parent_id="T-999")
    with pytest.raises(ValueError, match="own parent"):
        store.update_fields(child.id, actor="user", parent_id=child.id)
    t = store.update_fields(child.id, actor="user", parent_id="")
    assert t.parent_id is None


def test_agent_model_validation_and_clear(store):
    t = store.create_task("x", project_id="proj", agent_model="Opus")
    assert t.agent_model == "opus"
    t = store.update_fields(t.id, actor="user", agent_model="claude-haiku-5-5")
    assert t.agent_model == "haiku"
    assert store.get_task(t.id).history[-1].comment == "agent_model: opus → haiku"
    with pytest.raises(ValueError, match="unknown agent model"):
        store.update_fields(t.id, actor="user", agent_model="gpt")
    t = store.update_fields(t.id, actor="user", agent_model="")
    assert t.agent_model is None


def test_approval_fills_the_project_default_model(store):
    store.update_project("proj", default_model="haiku")
    t = store.create_task("x", project_id="proj")
    store.move_task(t.id, "analysis", actor="user")
    t = store.move_task(t.id, "approved", actor="user")
    assert t.agent_model == "haiku"
    full = store.get_task(t.id)
    assert full.history[-1].comment == "agent_model: — → haiku (project default)"
    # a model picked in Analysis is kept
    u = store.create_task("y", project_id="proj", agent_model="opus")
    assert store.move_task(u.id, "approved", actor="user").agent_model == "opus"
    # created straight into Approved also gets a model
    v = store.create_task("z", project_id="proj", status="approved")
    assert v.agent_model == "haiku"


def test_pull_claims_into_in_progress_and_binds_session(store):
    store.register_session("sess-1", "worker-3", project_id="proj")
    t = store.create_task("x", project_id="proj", status="approved")
    t = store.pull_task(t.id, assignee="worker-3", session_id="sess-1")
    assert t.status == "in_progress" and t.assignee == "worker-3"
    assert t.assigned_session_id == "sess-1" and t.run_count == 1
    assert store.get_session("sess-1").current_task_id == t.id


def test_agent_proposal_history_note(store):
    src = store.create_task("src", project_id="proj")
    t = store.create_task("follow-up", project_id="proj", origin="agent",
                          origin_task_id=src.id, actor="claude")
    assert t.origin == "agent" and t.origin_task_id == src.id
    assert t.history[0].comment == f"proposed by an agent while working on {src.id}"


# ---------------------------------------------------------------- sessions


def test_sessions_register_touch_and_offline(store):
    s = store.register_session("s1", "analyst-1", machine="box", model="Sonnet",
                               project_id="proj")
    assert (s.status, s.model, s.started_by) == ("active", "sonnet", "manual")
    t = store.create_task("x", project_id="proj")
    s = store.touch_session("s1", status="waiting_input", task_id=t.id, note="asking")
    assert (s.status, s.current_task_id, s.note) == ("waiting_input", t.id, "asking")
    # re-registering keeps started_at and the current card
    again = store.register_session("s1", "analyst-1")
    assert again.started_at == s.started_at and again.current_task_id == t.id
    with pytest.raises(KeyError):
        store.touch_session("nope")
    with pytest.raises(ValueError, match="session status"):
        store.touch_session("s1", status="sleeping")
    # a stale heartbeat reads as offline; a very old one drops off the list
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    store._conn.execute("UPDATE sessions SET last_heartbeat=? WHERE id='s1'", (old,))
    assert store.get_session("s1").status == "offline"
    ancient = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    store._conn.execute("UPDATE sessions SET last_heartbeat=? WHERE id='s1'", (ancient,))
    assert store.list_sessions() == []
    assert [x.id for x in store.list_sessions(max_age_s=None)] == ["s1"]


def test_finished_sessions_hidden_by_default(store):
    store.register_session("a", "w1", project_id="proj")
    store.register_session("b", "w2", project_id="proj")
    store.touch_session("b", status="finished")
    assert [s.id for s in store.list_sessions(project_id="proj")] == ["a"]
    assert {s.id for s in store.list_sessions(include_finished=True)} == {"a", "b"}


# ---------------------------------------------------------------- policy & rules


def test_agents_cannot_approve_or_analyse_by_default(monkeypatch):
    monkeypatch.delenv("KANBAN_MCP_HUMAN_ONLY", raising=False)
    for s in ("analysis", "approved", "done"):
        assert check_agent_may_use(s)
    for s in ("backlog", "in_progress", "results", "blocked"):
        assert check_agent_may_use(s) is None
    # old column names in the env var still bite
    monkeypatch.setenv("KANBAN_MCP_HUMAN_ONLY", "uat")
    assert check_agent_may_use("results")


def test_old_rules_with_pre_v6_columns_still_load(tmp_path):
    f = tmp_path / "rules.json"
    f.write_text(json.dumps({"rules": [
        {"name": "close accepted", "trigger": {"type": "task_idle", "status": "uat", "days": 7},
         "action": {"type": "move_to", "status": "Done"}},
        {"name": "nudge", "trigger": {"type": "task_idle", "status": "In Progress", "days": 1},
         "action": {"type": "add_comment", "comment": "still on it?"},
         "wip_limit": {"statuses": ["analyst", "UAT"], "max": 1}},
    ]}), encoding="utf-8")
    rules, errs, _ = rules_mod._load_rules(f)
    assert errs == []
    assert [r["trigger"]["status"] for r in rules] == ["results", "in_progress"]
    assert rules[0]["action"]["status"] == "done"
    assert rules[1]["wip_limit"]["statuses"] == ["analysis", "results"]


# ---------------------------------------------------------------- MCP


@pytest.fixture
def mcp_store(store, monkeypatch):
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.delenv("KANBAN_ACTOR", raising=False)
    monkeypatch.delenv("KANBAN_PROJECT_ID", raising=False)
    monkeypatch.delenv("KANBAN_MCP_HUMAN_ONLY", raising=False)
    monkeypatch.delenv("KANBAN_AGENT_MODEL", raising=False)
    monkeypatch.delenv("KANBAN_STARTED_BY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(server, "_session", {"id": None, "registered": False})
    return store


async def _call(name: str, **args):
    content, _structured = await server.mcp.call_tool(name, args)
    return json.loads(content[0].text)


@pytest.mark.asyncio
async def test_task_propose_lands_in_backlog_as_agent_proposal(mcp_store, monkeypatch):
    monkeypatch.setenv("KANBAN_ACTOR", "worker-3")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-123")
    src = mcp_store.create_task("Tune filter", project_id="proj", status="approved")
    pulled = await _call("kanban_pull", task_id=src.id)
    assert pulled["ok"] and pulled["data"]["status"] == "in_progress"
    r = await _call("task_propose", title="Replay viewer", type="story",
                    story_as_a="an operator", story_i_want="to replay missions",
                    story_so_that="I can check the filter",
                    acceptance="- [ ] opens a log")
    assert r["ok"], r
    d = r["data"]
    assert (d["status"], d["origin"], d["type"], d["project_id"]) == \
        ("backlog", "agent", "story", "proj")
    t = mcp_store.get_task(d["id"])
    # the source card defaults to what this session is working on
    assert t.origin_task_id == src.id and t.origin_session_id == "cc-123"
    # proposals cannot jump the queue
    with pytest.raises(Exception, match="unknown argument 'status'"):
        await _call("task_propose", title="x", status="approved")


@pytest.mark.asyncio
async def test_session_registers_itself_on_first_change(mcp_store, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-auto")
    monkeypatch.setenv("KANBAN_ACTOR", "worker-4")
    monkeypatch.setenv("KANBAN_PROJECT_ID", "proj")
    monkeypatch.setenv("KANBAN_AGENT_MODEL", "haiku")
    monkeypatch.setenv("KANBAN_STARTED_BY", "hub")
    assert mcp_store.get_session("cc-auto") is None
    r = await _call("kanban_create", title="x")
    assert r["ok"] and r["data"]["origin"] == "agent"
    s = mcp_store.get_session("cc-auto")
    assert (s.agent_name, s.project_id, s.model, s.started_by) == \
        ("worker-4", "proj", "haiku", "hub")
    st = await _call("session_status", status="waiting_input", task_id=r["data"]["id"],
                     note="which log format?")
    assert st["ok"] and st["data"]["status"] == "waiting_input"
    assert st["data"]["current_task_id"] == r["data"]["id"]


@pytest.mark.asyncio
async def test_session_register_without_client_session_id(mcp_store):
    r = await _call("session_register", agent_name="analyst-1", model="opus")
    assert r["ok"] and r["data"]["id"].startswith("s-")
    assert r["data"]["model"] == "opus"
    st = await _call("session_status", status="idle")
    assert st["ok"] and st["data"]["status"] == "idle"


@pytest.mark.asyncio
async def test_session_status_without_session_is_an_error(mcp_store):
    r = await _call("session_status", status="idle")
    assert not r["ok"] and "session_register" in r["error"]


@pytest.mark.asyncio
async def test_agents_finish_in_results_not_done(mcp_store):
    t = mcp_store.create_task("x", project_id="proj", status="in_progress")
    assert (await _call("kanban_move", task_id=t.id, to_status="results"))["ok"]
    r = await _call("kanban_move", task_id=t.id, to_status="approved")
    assert not r["ok"] and "reserved for a human" in r["error"]


# ---------------------------------------------------------------- REST


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        if main._store.get_project("mb") is None:
            main._store.create_project("mb", "Mission Board", default_model="opus")
        yield c


def test_rest_story_create_update_and_board(client):
    r = client.post("/api/tasks", json={
        "title": "Gyro bias", "project_id": "mb", "type": "story",
        "story_as_a": "an operator", "story_i_want": "online bias",
        "story_so_that": "less drift", "agent_model": "Sonnet",
    })
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["type"] == "story" and t["agent_model"] == "sonnet" and t["origin"] == "user"
    r = client.patch(f"/api/tasks/{t['id']}", json={
        "analysis_md": "Reuse the EKF state.", "agent_model": "haiku",
        "model_rationale": "small change",
    })
    assert r.status_code == 200, r.text
    u = r.json()
    assert u["analysis_md"] == "Reuse the EKF state." and u["agent_model"] == "haiku"
    assert u["analysed_by"] == "user"
    r = client.patch(f"/api/tasks/{t['id']}", json={"agent_model": "gpt"})
    assert r.status_code == 400
    board = client.get("/api/board", params={"project": "mb"}).json()
    card = next(c for c in board["tasks"]["backlog"] if c["id"] == t["id"])
    assert card["type"] == "story" and card["agent_model"] == "haiku"
    assert any(c["id"] == "testing" and c.get("hidden") for c in board["columns"])


def test_rest_approval_uses_project_default(client):
    t = client.post("/api/tasks", json={"title": "x", "project_id": "mb"}).json()
    r = client.post(f"/api/tasks/{t['id']}/move", json={"to_status": "approved"})
    assert r.status_code == 200 and r.json()["agent_model"] == "opus"


def test_rest_agents_via_http_mcp_cannot_approve(client):
    t = client.post("/api/tasks", json={"title": "x", "project_id": "mb"}).json()
    r = client.post(f"/api/tasks/{t['id']}/move", json={"to_status": "approved"},
                    headers={"X-Kanban-Via": main.MCP_VIA})
    assert r.status_code == 403
    r = client.post("/api/tasks", json={"title": "idea", "project_id": "mb"},
                    headers={"X-Kanban-Via": main.MCP_VIA})
    assert r.status_code == 201 and r.json()["origin"] == "agent"


def test_rest_sessions(client):
    r = client.post("/api/sessions", json={
        "id": "rest-1", "agent_name": "worker-9", "project_id": "mb",
        "model": "opus", "started_by": "hub", "machine": "box",
    })
    assert r.status_code == 201, r.text
    t = client.post("/api/tasks", json={"title": "x", "project_id": "mb"}).json()
    r = client.post("/api/sessions/rest-1/status",
                    json={"status": "waiting_input", "task_id": t["id"], "note": "?"})
    assert r.status_code == 200 and r.json()["current_task_id"] == t["id"]
    listed = client.get("/api/sessions", params={"project": "mb"}).json()["sessions"]
    assert [s["id"] for s in listed] == ["rest-1"]
    assert client.post("/api/sessions/none/status", json={}).status_code == 404
    assert client.post("/api/sessions", json={"id": "x", "agent_name": "a",
                                              "started_by": "robot"}).status_code == 400


def test_rest_project_models(client):
    r = client.patch("/api/projects/mb", json={"analyst_model": "Opus"})
    assert r.status_code == 200 and r.json()["analyst_model"] == "opus"
    assert client.patch("/api/projects/mb", json={"default_model": "x"}).status_code == 400
