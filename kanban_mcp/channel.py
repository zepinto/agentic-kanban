"""Channel push: deliver the human's messages into a live Claude Code session.

Claude Code *channels* (research preview) let an MCP server push events into
a running session instead of waiting for the agent to poll. With
``KANBAN_MCP_CHANNEL=1`` the stdio server:

* declares the experimental ``claude/channel`` capability and adds channel
  instructions, so Claude knows what ``<channel source="…">`` events mean;
* watches the board for unread messages addressed to its session (or left on
  a card the session holds) and emits each one as a
  ``notifications/claude/channel`` event, once per message;
* keeps the session's heartbeat fresh while the connection is open, so the
  board shows it live even when the agent is idle at the prompt.

The session must load the server as a channel, e.g.::

    claude --dangerously-load-development-channels server:kanban

Without that flag (or when an organization has channels turned off, or in
``claude -p`` runs) Claude Code silently drops the events and never says so.
A push therefore does **not** mark the message read: it stays unread until
the agent answers it with ``inbox_reply`` or reads it with ``inbox_get``, so
the piggybacked ``inbox_pending`` hint and the Stop hook still catch anything
the channel lost. The push is also opt-in for that reason.

Only messages from people are pushed by default (``KANBAN_CHANNEL_FROM=human``);
``any`` also pushes messages other agents send. Anything not pushed stays in
the inbox. The board is the trust boundary: run it on localhost or behind
your own auth, since whoever can post a message can put text in front of
the agent.

The transport is stock MCP: notifications are written to the same stdio
stream the SDK session uses, and only after the client has sent
``notifications/initialized``.
"""
from __future__ import annotations

import logging
import os
import socket
from typing import Any

import anyio
from mcp import types
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from kanban_store.store import is_machine_actor

log = logging.getLogger("kanban.mcp.channel")

CAPABILITY = "claude/channel"
METHOD = "notifications/claude/channel"

INSTRUCTIONS = (
    "Messages from the Mission Board arrive as <channel source=\"…\" input_id=\"…\" "
    "kind=\"…\" sender=\"…\" task_id=\"…\">text</channel>. They come from the "
    "person reviewing your work (kind: instruction, comment on a report or figure, "
    "feedback on a result, answer to your question). Treat each one as a new "
    "instruction for the card in task_id: act on it, then answer with "
    "inbox_reply(input_id=…, text=…) saying what you did or will do — replying "
    "marks the message handled; until then it also shows up as inbox_pending."
)


def enabled() -> bool:
    return os.environ.get("KANBAN_MCP_CHANNEL", "").strip().lower() in ("1", "true", "yes", "on")


def push_from() -> str:
    v = os.environ.get("KANBAN_CHANNEL_FROM", "human").strip().lower()
    return v if v in ("human", "any") else "human"


def poll_interval() -> float:
    try:
        return max(0.2, float(os.environ.get("KANBAN_CHANNEL_POLL", "2")))
    except ValueError:
        return 2.0


def default_session_id() -> str:
    """A stable id for this connection when Claude Code didn't give one."""
    host = socket.gethostname().split(".")[0] or "host"
    return f"channel-{host}-{os.getpid()}"


# ---------------------------------------------------------------- pure helpers


def collect(store, session_id: str, *, sender: str = "human", seen: set[int] | None = None) -> list:
    """Unread messages to push now, skipping those already pushed (``seen``,
    updated in place). Nothing is marked read — see the module docstring."""
    seen = set() if seen is None else seen
    msgs = store.inbox(session_id, mark_delivered=False)
    keep = [m for m in msgs
            if m.id not in seen and (sender == "any" or not is_machine_actor(m.author))]
    seen.update(m.id for m in keep)
    return keep


def _ident_safe(v: Any) -> str:
    return " ".join(str(v).split())


def to_params(m) -> dict[str, Any]:
    """``notifications/claude/channel`` params for one board message.
    Meta keys are identifiers (Claude Code drops any other key)."""
    meta = {"input_id": str(m.id), "kind": m.kind, "sender": m.author}
    for key in ("task_id", "report_id", "asset_id"):
        val = getattr(m, key, None)
        if val is not None:
            meta[key] = str(val)
    about = getattr(m, "about", None)
    if about:
        meta["about"] = _ident_safe(about)
    return {"content": m.body, "meta": meta}


def notification(params: dict[str, Any]) -> SessionMessage:
    return SessionMessage(message=types.JSONRPCMessage(
        types.JSONRPCNotification(jsonrpc="2.0", method=METHOD, params=params)))


# ---------------------------------------------------------------- run loop


def _is_initialized(msg: Any) -> bool:
    if not isinstance(msg, SessionMessage):
        return False
    root = msg.message.root
    return isinstance(root, types.JSONRPCNotification) and root.method == "notifications/initialized"


async def serve(mcp, *, get_store, session: dict[str, Any], heartbeat, read=None, write=None) -> None:
    """Run ``mcp`` (a FastMCP) over stdio — or the given streams — with the
    channel capability and the push loop.

    ``session`` is the server's mutable session record (``{"id", "registered"}``);
    ``heartbeat`` registers/touches it. ``read``/``write`` are for tests.
    """
    low = mcp._mcp_server
    low.instructions = "\n\n".join(x for x in (low.instructions, INSTRUCTIONS) if x)
    init = low.create_initialization_options(experimental_capabilities={CAPABILITY: {}})

    if not session.get("id") and not (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip():
        session["id"] = (os.environ.get("KANBAN_SESSION_ID") or "").strip() or default_session_id()

    async def run(read_stream, write_stream) -> None:
        ready = anyio.Event()
        fwd_send, fwd_recv = anyio.create_memory_object_stream(100)

        async def tee() -> None:
            async with fwd_send:
                async for msg in read_stream:
                    if not ready.is_set() and _is_initialized(msg):
                        ready.set()
                    await fwd_send.send(msg)

        async def push() -> None:
            await ready.wait()
            sender = push_from()
            seen: set[int] = set()
            beat = 0.0
            while True:
                sid = session.get("id") or (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
                if sid:
                    try:
                        if beat <= 0:
                            await anyio.to_thread.run_sync(heartbeat)
                            beat = 30.0
                        msgs = await anyio.to_thread.run_sync(
                            lambda: collect(get_store(), sid, sender=sender, seen=seen))
                        for m in msgs:
                            await write_stream.send(notification(to_params(m)))
                    except anyio.ClosedResourceError:
                        return
                    except Exception:  # noqa: BLE001 — never take the server down
                        log.debug("channel push failed", exc_info=True)
                interval = poll_interval()
                beat -= interval
                await anyio.sleep(interval)

        async with anyio.create_task_group() as tg:
            tg.start_soon(tee)
            tg.start_soon(push)
            await low.run(fwd_recv, write_stream, init)
            tg.cancel_scope.cancel()

    if read is not None and write is not None:
        await run(read, write)
        return
    async with stdio_server() as (read_stream, write_stream):
        await run(read_stream, write_stream)
