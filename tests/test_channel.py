"""Channel push (KANBAN_MCP_CHANNEL=1): raw JSON-RPC over the real stdio server."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from kanban_mcp import channel
from kanban_store import Store

ROOT = Path(__file__).resolve().parent.parent


class Proc:
    def __init__(self, env: dict[str, str]):
        self.p = subprocess.Popen(
            [sys.executable, "-m", "kanban_mcp"], cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    def send(self, msg: dict):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def wait_for(self, pred, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                m = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if pred(m):
                return m
        raise AssertionError("timed out waiting for message")

    def close(self):
        self.p.stdin.close()
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env.update(PYTHONPATH=str(ROOT), KANBAN_DB=str(tmp_path / "ch.db"), KANBAN_CHANNEL_POLL="0.2",
               KANBAN_ACTOR="claude", **extra)
    return env


def _handshake(proc: Proc) -> dict:
    proc.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}}})
    init = proc.wait_for(lambda m: m.get("id") == 1)
    proc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return init["result"]


def test_channel_pushes_human_messages(tmp_path):
    store = Store(tmp_path / "ch.db")
    store.create_project("p", "P")
    t = store.create_task("x", project_id="p", status="in_progress")
    proc = Proc(_env(tmp_path, KANBAN_MCP_CHANNEL="1", KANBAN_SESSION_ID="cc-ch",
                     KANBAN_PROJECT_ID="p"))
    try:
        res = _handshake(proc)
        assert res["capabilities"]["experimental"] == {"claude/channel": {}}
        assert "<channel" in res["instructions"]
        # the connection registers the session (live on the board)
        for _ in range(50):
            if store.get_session("cc-ch"):
                break
            time.sleep(0.1)
        assert store.get_session("cc-ch").project_id == "p"
        store.send_input("from a bot", author="claude-2", session_id="cc-ch", task_id=t.id)
        m = store.send_input("label the axes", author="user", session_id="cc-ch", task_id=t.id)
        ev = proc.wait_for(lambda x: x.get("method") == "notifications/claude/channel")
        assert ev["params"]["content"] == "label the axes"
        meta = ev["params"]["meta"]
        meta.pop("about", None)
        assert meta == {"input_id": str(m.id), "kind": "instruction", "sender": "user",
                        "task_id": t.id}
        assert all(k.replace("_", "").isalnum() for k in meta)
        # pushed once, but left unread until the agent replies (a channel the
        # client did not load drops events silently)
        time.sleep(0.6)
        assert proc.q.empty() or all(x.get("method") != "notifications/claude/channel"
                                     for x in list(proc.q.queue))
        assert [x.body for x in store.inbox("cc-ch", mark_delivered=False)] == [
            "from a bot", "label the axes"]
        # tools still work over the same stream
        proc.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tl = proc.wait_for(lambda x: x.get("id") == 2)
        assert any(tool["name"] == "inbox_reply" for tool in tl["result"]["tools"])
    finally:
        proc.close()


def test_channel_off_by_default(tmp_path):
    proc = Proc(_env(tmp_path))
    try:
        res = _handshake(proc)
        assert "claude/channel" not in (res["capabilities"].get("experimental") or {})
    finally:
        proc.close()


def test_collect_and_params(store):
    store.register_session("s1", "w", project_id="proj")
    t = store.create_task("x", project_id="proj", status="in_progress")
    store.send_input("bot", author="codex", session_id="s1")
    store.send_input("hi", author="ana", session_id="s1", task_id=t.id)
    seen: set[int] = set()
    got = channel.collect(store, "s1", seen=seen)
    assert [m.body for m in got] == ["hi"]
    assert channel.collect(store, "s1", seen=seen) == []
    got = channel.collect(store, "s1", sender="any", seen=seen)
    assert [m.body for m in got] == ["bot"]
    assert store.pending_inputs("s1") == 2          # nothing marked read
    p = channel.to_params(got[0])
    assert p["content"] == "bot" and "task_id" not in p["meta"]


def test_connect_channel_flag(tmp_path):
    from kanban_mcp.connect import write_mcp_json
    d = tmp_path / "app"
    d.mkdir()
    _f, name, cfg = write_mcp_json(d, "p", db=tmp_path / "x.db", channel=True)
    assert cfg["mcpServers"][name]["env"]["KANBAN_MCP_CHANNEL"] == "1"
    _f, _n, cfg = write_mcp_json(d, "p", db=tmp_path / "x.db")           # kept
    assert cfg["mcpServers"][name]["env"]["KANBAN_MCP_CHANNEL"] == "1"
    _f, _n, cfg = write_mcp_json(d, "p", db=tmp_path / "x.db", channel=False)
    assert "KANBAN_MCP_CHANNEL" not in cfg["mcpServers"][name]["env"]
