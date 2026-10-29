"""Reports (mermaid / plotly / snapshot / svg / file assets), figure comments,
review, analysis requests and the task_event rule trigger."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from kanban_mcp import server
from kanban_store.reports import prepare_asset
from kanban_ui import main
from kanban_ui.automation import rules as rules_mod

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
SVG = '<?xml version="1.0"?><!-- drawn by hand --><svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
FIG = {"data": [{"x": [1, 2, 3], "y": [2, 1, 3]}], "layout": {"title": {"text": "t"}}}


# ---------------------------------------------------------------- asset validation


def test_asset_types_are_special_cased():
    t, cap, body, mime, name = prepare_asset({"type": "mermaid", "text": "```mermaid\ngraph LR; A-->B\n```"})
    assert (t, body, mime) == ("mermaid", b"graph LR; A-->B", "text/plain; charset=utf-8")
    t, _, body, mime, _ = prepare_asset({"type": "plotly", "figure": FIG})
    assert mime == "application/json" and json.loads(body)["data"][0]["y"] == [2, 1, 3]
    t, _, body, mime, _ = prepare_asset({"type": "plotly", "text": json.dumps(FIG)})
    assert t == "plotly"
    t, _, _, mime, name = prepare_asset({"type": "screenshot", "data": PNG})
    assert (t, mime, name) == ("snapshot", "image/png", "snapshot.png")
    t, _, _, mime, _ = prepare_asset({"type": "svg", "text": SVG})
    assert mime == "image/svg+xml"
    t, _, _, mime, name = prepare_asset({"type": "file", "data": b"a,b", "filename": "../x.csv"})
    assert (mime, name) == ("application/octet-stream", "x.csv")


@pytest.mark.parametrize("spec, msg", [
    ({"type": "snapshot", "data": b"not an image"}, "PNG, JPEG, GIF or WebP"),
    ({"type": "snapshot", "data": SVG.encode()}, "type 'svg'"),
    ({"type": "plotly", "figure": {"layout": {}}}, '"data" list'),
    ({"type": "plotly", "text": "{nope"}, "not valid JSON"),
    ({"type": "svg", "text": "<html><svg/></html>"}, "root element"),
    ({"type": "mermaid", "text": "  "}, "diagram source"),
    ({"type": "video"}, "must be one of"),
])
def test_asset_validation_errors(spec, msg):
    with pytest.raises(ValueError, match=msg):
        prepare_asset(spec)


# ---------------------------------------------------------------- store


def _report(store, task_id, **kw):
    return store.create_report(task_id, kw.pop("title", "Drift study"), author="worker-3", assets=[
        {"type": "mermaid", "text": "graph LR; A-->B", "caption": "flow"},
        {"type": "plotly", "figure": FIG, "caption": "bias"},
        {"type": "snapshot", "data": PNG, "caption": "shot"},
        {"type": "svg", "text": SVG, "caption": "geometry"},
    ], **kw)


def test_report_assets_are_content_addressed(store):
    t = store.create_task("x", project_id="proj", status="in_progress")
    r1 = _report(store, t.id)
    r2 = _report(store, t.id, title="again")
    assert [a.type for a in r1.assets] == ["mermaid", "plotly", "snapshot", "svg"]
    assert r1.assets[2].sha256 == r2.assets[2].sha256          # stored once
    assert store.asset_bytes(r1.assets[2]) == PNG
    assert store.asset_file(r1.assets[0]).parent.parent == store.assets_dir
    hist = store.get_task(t.id).history[-1]
    assert hist.action == "report" and "progress report: again (4 assets)" == hist.comment


def test_results_supersede_and_review(store):
    t = store.create_task("x", project_id="proj", status="in_progress")
    store.register_session("w3", "worker-3", project_id="proj")
    old = _report(store, t.id, kind="result", session_id="w3")
    new = _report(store, t.id, kind="result", title="v2", session_id="w3")
    assert store.get_report(old.id).review_status == "superseded"
    assert new.review_status == "submitted"
    store.move_task(t.id, "results", actor="worker-3")
    with pytest.raises(ValueError, match="what should change"):
        store.review_report(new.id, "changes", actor="user")
    r = store.review_report(new.id, "changes", actor="user", comment="label the axes")
    assert r.review_status == "changes_requested"
    assert store.get_task(t.id).status == "in_progress"
    msg = store.inbox("w3")[0]
    assert msg.kind == "feedback" and "label the axes" in msg.body and msg.report_id == new.id
    final = _report(store, t.id, kind="result", title="v3", session_id="w3")
    store.move_task(t.id, "results", actor="worker-3")
    assert store.review_report(final.id, "accept", actor="user").review_status == "accepted"
    assert store.get_task(t.id).status == "done"
    prog = _report(store, t.id, title="progress")
    with pytest.raises(ValueError, match="only result"):
        store.review_report(prog.id, "accept", actor="user")


def test_figure_comment_reaches_the_posting_agent(store):
    t = store.create_task("x", project_id="proj", status="in_progress")
    store.register_session("w4", "worker-4", project_id="proj")
    r = _report(store, t.id, session_id="w4")
    m = store.send_input("why flat?", author="user", asset_id=r.assets[1].id)
    assert (m.session_id, m.task_id, m.kind, m.report_id) == ("w4", t.id, "comment", r.id)
    assert m.about == "figure “bias” in report “Drift study”"
    assert ("message", "to worker-4 on figure “bias” in report “Drift study”: why flat?") in [
        (h.action, h.comment) for h in store.get_task(t.id).history]
    with pytest.raises(KeyError):
        store.send_input("x", author="user", asset_id=9999)


def test_report_summary_prefers_images_for_thumbnails(store):
    t = store.create_task("x", project_id="proj")
    r = _report(store, t.id)
    info = store.report_summary([t.id])[t.id]
    assert info["reports"] == 1 and info["thumb"]["asset_id"] == r.assets[2].id
    gallery = store.list_project_assets("proj", kind=None)
    assert {a["type"] for a in gallery} == {"mermaid", "plotly", "snapshot", "svg"}
    assert store.list_project_assets("proj") == []           # no result reports yet


def test_analysis_request_and_submission(store):
    store.update_project("proj", analyst_model="opus")
    t = store.create_task("story", project_id="proj", type="story")
    t2, model = store.request_analysis(t.id, actor="user")
    assert t2.status == "analysis" and model == "opus"
    assert store.get_task(t.id).history[-1].action == "analysis_requested"
    done, subs = store.submit_analysis(
        t.id, actor="analyst", analysis_md="two extra EKF states", agent_model="Sonnet",
        model_rationale="one module", acceptance="- [ ] converges",
        subtasks=[{"title": "Replay UI", "size": "S"}],
    )
    assert (done.analysed_by, done.agent_model, done.status) == ("analyst", "sonnet", "analysis")
    assert subs[0].parent_id == t.id and subs[0].status == "analysis" and subs[0].origin == "agent"
    with pytest.raises(ValueError, match="unknown agent model"):
        store.submit_analysis(t.id, actor="a", analysis_md="x", agent_model="gpt")
    store.move_task(t.id, "approved", actor="user")
    with pytest.raises(ValueError, match="before approval"):
        store.request_analysis(t.id, actor="user")


# ---------------------------------------------------------------- MCP


@pytest.fixture
def mcp_store(store, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_store", store)
    for k in ("KANBAN_ACTOR", "KANBAN_PROJECT_ID", "KANBAN_MCP_HUMAN_ONLY", "KANBAN_AGENT_MODEL",
              "KANBAN_STARTED_BY", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(server, "_session", {"id": None, "registered": False})
    monkeypatch.setenv("PWD", str(tmp_path))
    return store


async def _call(name: str, **args):
    content, _structured = await server.mcp.call_tool(name, args)
    return json.loads(content[0].text)


@pytest.mark.asyncio
async def test_report_post_from_files_and_inline(mcp_store, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-rep")
    monkeypatch.setenv("KANBAN_PROJECT_ID", "proj")
    (tmp_path / "plot.png").write_bytes(PNG)
    (tmp_path / "fig.json").write_text(json.dumps(FIG))
    (tmp_path / "geo.svg").write_text(SVG)
    t = mcp_store.create_task("x", project_id="proj", status="approved")
    await _call("kanban_pull", task_id=t.id)
    r = await _call("report_post", title="Heading drift halved", final=True,
                    summary_md="Replayed **6** missions", assets=[
                        {"type": "snapshot", "path": "plot.png", "caption": "replay"},
                        {"type": "plotly", "path": str(tmp_path / "fig.json")},
                        {"type": "svg", "path": "geo.svg"},
                        {"type": "mermaid", "source": "graph TD; A-->B"},
                        {"type": "plotly", "figure": FIG},
                    ])
    assert r["ok"], r
    assert r["data"]["moved_to_results"] and r["data"]["task_id"] == t.id
    assert [a["type"] for a in r["data"]["assets"]] == ["snapshot", "plotly", "svg", "mermaid", "plotly"]
    rep = mcp_store.get_report(r["data"]["report_id"])
    assert rep.kind == "result" and rep.session_id == "cc-rep" and rep.author == "claude"
    assert mcp_store.get_task(t.id).status == "results"


@pytest.mark.asyncio
async def test_report_post_errors_are_actionable(mcp_store):
    t = mcp_store.create_task("x", project_id="proj")
    r = await _call("report_post", task_id=t.id, title="x",
                    assets=[{"type": "snapshot", "path": "/nope/missing.png"}])
    assert not r["ok"] and "cannot read" in r["error"]
    r = await _call("report_post", task_id=t.id, title="x",
                    assets=[{"type": "plotly", "path": "chart.html"}])
    assert not r["ok"] and "as JSON" in r["error"]
    r = await _call("report_post", task_id=t.id, title="x", assets=[{"type": "svg", "src": "<svg/>"}])
    assert not r["ok"] and "unknown key" in r["error"]
    r = await _call("report_post", title="x")
    assert not r["ok"] and "task_id is required" in r["error"]


@pytest.mark.asyncio
async def test_analysis_submit_tool(mcp_store):
    t = mcp_store.create_task("x", project_id="proj", status="analysis")
    r = await _call("analysis_submit", task_id=t.id, analysis_md="scope", agent_model="haiku",
                    model_rationale="tiny", subtasks=[{"title": "part 2"}], actor="analyst")
    assert r["ok"] and r["data"]["task"]["agent_model"] == "haiku"
    assert r["data"]["subtasks"][0]["parent_id"] == t.id


# ---------------------------------------------------------------- REST


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        if main._store.get_project("rep") is None:
            main._store.create_project("rep", "Reports", analyst_model="opus")
        yield c


def test_rest_reports_assets_review_gallery(client):
    t = client.post("/api/tasks", json={"title": "drift", "project_id": "rep",
                                        "status": "in_progress"}).json()
    body = {"title": "Result", "final": True, "summary_md": "ok", "assets": [
        {"type": "snapshot", "data_base64": base64.b64encode(PNG).decode(), "caption": "shot"},
        {"type": "svg", "text": SVG},
        {"type": "plotly", "figure": FIG},
        {"type": "mermaid", "text": "graph LR; A-->B"},
        {"type": "file", "text": "a,b\n1,2", "filename": "n.csv"},
    ]}
    r = client.post(f"/api/tasks/{t['id']}/reports", json=body)
    assert r.status_code == 201, r.text
    rep = r.json()
    assert client.get(f"/api/tasks/{t['id']}").json()["status"] == "results"
    ids = {a["type"]: a["id"] for a in rep["assets"]}
    img = client.get(f"/api/assets/{ids['snapshot']}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert img.content == PNG and "immutable" in img.headers["cache-control"]
    svg = client.get(f"/api/assets/{ids['svg']}")
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in svg.headers["content-security-policy"]
    assert svg.headers["x-content-type-options"] == "nosniff"
    f = client.get(f"/api/assets/{ids['file']}")
    assert f.headers["content-disposition"].startswith("attachment")
    assert client.get(f"/api/assets/{ids['plotly']}").json()["data"][0]["x"] == [1, 2, 3]
    assert client.get(f"/api/assets/{ids['mermaid']}").text == "graph LR; A-->B"
    board = client.get("/api/board", params={"project": "rep"}).json()
    card = next(c for c in board["tasks"]["results"] if c["id"] == t["id"])
    assert card["reports"] == 1 and card["review_status"] == "submitted"
    assert card["thumb"]["type"] == "snapshot"
    g = client.get("/api/reports", params={"project": "rep"}).json()["assets"]
    assert {a["type"] for a in g} == {"snapshot", "svg", "plotly", "mermaid"}
    m = client.post(f"/api/tasks/{t['id']}/messages", json={"text": "axes?", "asset_id": ids["plotly"]})
    assert m.status_code == 201 and m.json()["about"].startswith("figure")
    # agents (HTTP MCP) cannot review; humans can
    rv = client.post(f"/api/reports/{rep['id']}/review", json={"decision": "accept"},
                     headers={"X-Kanban-Via": main.MCP_VIA})
    assert rv.status_code == 403
    rv = client.post(f"/api/reports/{rep['id']}/review", json={"decision": "accept"})
    assert rv.status_code == 200 and rv.json()["review_status"] == "accepted"
    assert client.get(f"/api/tasks/{t['id']}").json()["status"] == "done"


def test_rest_report_validation(client):
    t = client.post("/api/tasks", json={"title": "x", "project_id": "rep"}).json()
    r = client.post(f"/api/tasks/{t['id']}/reports", json={
        "title": "x", "assets": [{"type": "snapshot", "data_base64": "%%%"}]})
    assert r.status_code == 400 and "base64" in r.text
    r = client.post(f"/api/tasks/{t['id']}/reports", json={
        "title": "x", "assets": [{"type": "plotly", "text": "{}"}]})
    assert r.status_code == 400 and "assets[0]" in r.text
    assert client.post("/api/tasks/T-9999/reports", json={"title": "x"}).status_code == 404
    assert client.get("/api/assets/999999").status_code == 404


def test_rest_analyse(client):
    t = client.post("/api/tasks", json={"title": "x", "project_id": "rep"}).json()
    r = client.post(f"/api/tasks/{t['id']}/analyse")
    assert r.status_code == 200 and r.json()["analyst_model"] == "opus"
    assert r.json()["task"]["status"] == "analysis"
    assert client.post(f"/api/tasks/{t['id']}/analyse",
                       headers={"X-Kanban-Via": main.MCP_VIA}).status_code == 403


# ---------------------------------------------------------------- task_event rules


def test_task_event_rules_validate_and_match(store, tmp_path, monkeypatch):
    f = tmp_path / "rules.json"
    f.write_text(json.dumps({"rules": [
        {"name": "analyst", "trigger": {"type": "task_event", "action": "analysis_requested"},
         "action": {"type": "add_comment", "comment": "analyst started"}},
        {"name": "resume", "trigger": {"type": "task_event", "action": "message", "agent": "offline"},
         "action": {"type": "add_comment", "comment": "resuming"}},
        {"name": "bad", "trigger": {"type": "task_event", "action": "nap"},
         "action": {"type": "add_comment", "comment": "x"}},
    ]}), encoding="utf-8")
    rules, errs, _ = rules_mod._load_rules(f)
    assert [r["name"] for r in rules] == ["analyst", "resume"]
    assert any("task_event.action" in e for e in errs)

    engine = rules_mod.RuleEngine(store, f)
    engine._rules = rules
    monkeypatch.setitem(rules_mod._status, "paused", False)
    t = store.create_task("x", project_id="proj")
    task = store.get_task(t.id).to_public()
    out = rules_mod.emit_rule_event("task_event", {"task": task, "action": "analysis_requested",
                                                   "actor": "user"})
    assert out == ["comment: analyst started"]
    # a human message while no agent is on the card → resume rule fires
    out = rules_mod.emit_rule_event("task_event", {"task": task, "action": "message", "actor": "user"})
    assert out == ["comment: resuming"]
    # an agent's own message never triggers it (default from=human)
    assert rules_mod.emit_rule_event("task_event", {"task": task, "action": "message",
                                                    "actor": "claude"}) == []
    # with a live session on the card, nothing to resume
    store.register_session("live", "worker", project_id="proj", current_task_id=t.id)
    assert rules_mod.emit_rule_event("task_event", {"task": task, "action": "message",
                                                    "actor": "user"}) == []
