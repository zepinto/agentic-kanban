"""Claude Code hook: deliver the human's board messages to the agent.

Wire it into a project's ``.claude/settings.json`` (or the launcher's run
settings)::

    {"hooks": {
      "Stop":             [{"hooks": [{"type": "command",
                             "command": "<repo>/.venv/bin/python -m kanban_mcp.hook"}]}],
      "UserPromptSubmit": [{"hooks": [{"type": "command",
                             "command": "<repo>/.venv/bin/python -m kanban_mcp.hook"}]}]
    }}

Claude Code passes the event as JSON on stdin (``session_id``,
``hook_event_name``). The hook reads that session's unread messages from the
board and

* on ``Stop``: if any are waiting, blocks the stop and hands them over as the
  reason, so the agent acts on them instead of ending its turn;
* on ``UserPromptSubmit`` / ``SessionStart``: prints them, which Claude Code
  adds to the conversation as context.

With nothing waiting it exits 0 silently. The board is read directly from
``KANBAN_DB`` when set (same machine), else over HTTP from ``KANBAN_URL``
(default ``http://localhost:7777``). Any failure is swallowed: a hook must
never break the agent's session.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _fetch(session_id: str) -> list[dict[str, Any]]:
    db = os.environ.get("KANBAN_DB", "").strip()
    if db:
        from kanban_store import Store

        store = Store(db, create=False)
        try:
            if store.get_session(session_id) is None:
                return []
            return [m.to_public() for m in store.inbox(session_id)]
        finally:
            store.close()
    base = os.environ.get("KANBAN_URL", "http://localhost:7777").rstrip("/")
    url = f"{base}/api/sessions/{urllib.parse.quote(session_id)}/inbox"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 — local board URL
            return json.load(r).get("messages", [])
    except urllib.error.HTTPError as e:
        if e.code == 404:  # session not on the board (yet)
            return []
        raise


def _render(msgs: list[dict[str, Any]]) -> str:
    lines = ["Messages from the human on the kanban board:"]
    for m in msgs:
        about = f" about {m['task_id']}" if m.get("task_id") else ""
        if m.get("about"):
            about += f", on {m['about']}"
        lines.append(f"- [#{m['id']} {m['kind']}{about}] {m['body']}")
    lines.append(
        "Act on these now. Answer each with inbox_reply(input_id=<#id>, text=...) "
        "so the human sees your reply on the card."
    )
    return "\n".join(lines)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0
    session_id = str(event.get("session_id") or "").strip()
    name = event.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not session_id:
        return 0
    try:
        msgs = _fetch(session_id)
    except Exception:  # noqa: BLE001
        return 0
    if not msgs:
        return 0
    text = _render(msgs)
    if name == "Stop" or name == "SubagentStop":
        print(json.dumps({"decision": "block", "reason": text}))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
