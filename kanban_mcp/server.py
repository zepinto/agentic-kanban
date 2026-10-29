"""agent-kanban — MCP server.

stdio transport. Registered in ``~/.claude.json`` (Claude Code) or
``.mcp.json`` (workspace scope) using the format:

    {
      "mcpServers": {
        "agent-kanban": {
          "type": "stdio",
          "command": "<repo>/.venv/bin/python",
          "args":    ["-m", "kanban_mcp"],
          "cwd":     "<repo>",
          "env":     {"PYTHONPATH": "<repo>", "KANBAN_DB": "<repo>/tasks.db",
                      "KANBAN_PROJECT_ID": "myproj", "KANBAN_ACTOR": "claude"}
        }
      }
    }

The same setup works for Cline (Settings → MCP Servers → Add).

Environment:

* ``KANBAN_DB``         — SQLite file shared with the web UI.
* ``KANBAN_PROJECT_ID`` — this agent's project: default for ``kanban_create``
  and the scope of list/search/board/my_active (pass ``project_id="*"`` to
  look across all projects).
* ``KANBAN_ACTOR``      — name written to history for this agent's changes
  (default ``claude``); a tool's ``actor`` argument overrides it.
* ``KANBAN_MCP_HUMAN_ONLY`` — comma-separated statuses agents may not move
  or create cards into (default ``analysis,approved,done``: refining,
  approving and closing are the human's calls). Set to an empty string to
  allow everything.
* ``KANBAN_URL``        — base URL of the web UI for card links
  (default ``http://localhost:7777``).
* ``CLAUDE_CODE_SESSION_ID`` — set by Claude Code for its MCP servers. When
  present, the session registers itself on the board on its first change
  and every change counts as a heartbeat (no need to call session_register).
* ``KANBAN_AGENT_MODEL`` / ``KANBAN_STARTED_BY`` — what the launcher started
  this session with (``opus``/``sonnet``/``haiku``; ``hub``), shown on the
  sessions strip.
* ``KANBAN_MCP_CHANNEL=1`` — push the human's messages into the session as
  Claude Code channel events (start ``claude`` with
  ``--dangerously-load-development-channels server:<name>``); see
  ``kanban_mcp/channel.py``. ``KANBAN_CHANNEL_FROM`` (``human``/``any``),
  ``KANBAN_CHANNEL_POLL`` (seconds, default 2) and ``KANBAN_SESSION_ID``
  (session id when Claude Code gives none) tune it.

Tool arguments are strict: an unknown argument name (``project`` instead of
``project_id``, ``body`` instead of ``description``) is an error with a
suggestion, not a silently dropped value.

Tools:

* ``kanban_columns``   — column descriptions (so the agent gets oriented)
* ``kanban_projects``  — projects with task counts
* ``kanban_board``     — compact board overview of one project
* ``kanban_list``      — list tasks (filter by status / assignee / project)
* ``kanban_search``    — substring search in title/description
* ``kanban_my_active`` — the agent's tasks in in_progress/testing
* ``kanban_get``       — full card with the newest history rows
* ``kanban_history``   — older history, page by page
* ``kanban_pull``      — atomically "claim a task" (approved → in_progress)
* ``kanban_move``      — move a task to a new status
* ``kanban_assign``    — set or clear the assignee
* ``kanban_comment``   — comment in history
* ``kanban_create``    — new card
* ``kanban_update``    — edit fields
* ``kanban_link``      — add a link (memory / file / pr / url / plan)
* ``kanban_blockers``  — replace internal blockers (dependencies)
* ``task_propose``     — propose a task or user story; always lands in Backlog
* ``session_register`` — announce this agent session on the board
* ``session_status``   — report status / current card / a short note
* ``inbox_get``        — read the human's messages to this session (can wait)
* ``inbox_reply``      — answer a message; the reply shows on the card
* ``analysis_submit``  — the analyst agent's notes, criteria, model pick, subtasks
* ``report_post``      — post a progress update or a final result with figures
  (mermaid diagrams, Plotly charts, snapshots, SVG, files)

Changes made here land in ``task_history``; the web server's event feed
picks them up within about a second, so automation rules and webhooks fire
for agent moves exactly as for drag-drops in the UI (the web server must be
running for that part).

On failure a human-readable message is returned; the MCP layer does not crash.
"""
from __future__ import annotations

import difflib
import logging
import os
import socket
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ConfigDict, model_validator

from kanban_mcp.policy import check_agent_may_use as _check_agent_may_use
from kanban_store import Store, STATUSES, StatusConflict, status_meta
from kanban_store.store import DEFAULT_PROJECT_ID, LINK_TYPES, normalize_status

log = logging.getLogger("kanban.mcp")


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_store: Store | None = None

# History rows returned by kanban_get unless asked otherwise. A long-lived
# card can have thousands of rows; dumping them all into an agent's context
# is expensive and rarely useful.
DEFAULT_HISTORY_LIMIT = 30


def _get_store() -> Store:
    global _store
    if _store is None:
        # Never create a fresh database here: a wrong KANBAN_DB must be an
        # error the agent reports, not a private empty board.
        _store = Store(create=False)
    return _store


def _actor(explicit: str | None) -> str:
    # Every mutating tool resolves its actor: a natural place for the heartbeat.
    _heartbeat()
    return (explicit or "").strip() or os.environ.get("KANBAN_ACTOR", "").strip() or "claude"


# ---------------------------------------------------------------------------
# Agent session (one per MCP server process = one Claude Code session)
# ---------------------------------------------------------------------------

_session: dict[str, Any] = {"id": None, "registered": False}


def _session_id() -> str | None:
    return _session["id"] or (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip() or None


def _session_defaults() -> dict[str, Any]:
    return {
        "agent_name": os.environ.get("KANBAN_ACTOR", "").strip() or "claude",
        "machine": socket.gethostname(),
        "repo": os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD") or "",
        "project_id": os.environ.get("KANBAN_PROJECT_ID") or None,
        "model": os.environ.get("KANBAN_AGENT_MODEL") or None,
        "started_by": "hub" if os.environ.get("KANBAN_STARTED_BY") == "hub" else "manual",
    }


def _heartbeat() -> None:
    """Refresh this session's heartbeat; registers it on first use when
    Claude Code told us its session id. Never fails the calling tool."""
    sid = _session_id()
    if not sid:
        return
    try:
        if _session["registered"]:
            _get_store().touch_session(sid)
            return
        d = _session_defaults()
        if d["project_id"] and _get_store().get_project(d["project_id"]) is None:
            d["project_id"] = None
        try:
            _get_store().register_session(sid, **d)
        except ValueError:
            d["model"] = None
            _get_store().register_session(sid, **d)
        _session.update(id=sid, registered=True)
    except KeyError:
        _session["registered"] = False
    except Exception:  # noqa: BLE001 — the board view is best-effort
        log.debug("session heartbeat failed", exc_info=True)


def _scope(project_id: str | None) -> str | None:
    """Project filter for read tools: explicit slug, "*" for all projects,
    or this agent's KANBAN_PROJECT_ID."""
    if project_id in ("*", "all"):
        return None
    return project_id or os.environ.get("KANBAN_PROJECT_ID") or None


def _url(task_id: str) -> str:
    base = os.environ.get("KANBAN_URL", "http://localhost:7777").rstrip("/")
    return f"{base}/t/{task_id}"


def _err(msg: str) -> dict[str, Any]:
    return {"ok": False, "error": msg}


def _ok(payload: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "data": payload}
    # Piggyback: whatever the agent called, tell it the human is waiting.
    n = _inbox_pending()
    if n:
        out["inbox_pending"] = n
        out["inbox_hint"] = (
            f"{n} unread message(s) from the human — call inbox_get now and "
            "act on them before continuing."
        )
    return out


def _inbox_pending() -> int:
    sid = _session_id() if "_session_id" in globals() else None
    if not sid:
        return 0
    try:
        return _get_store().pending_inputs(sid)
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP("agent-kanban")


@mcp.tool()
def kanban_columns() -> dict[str, Any]:
    """Describe every kanban column and who moves cards in/out of it.

    Call this at the start of a session to understand the current status model.
    """
    return _ok({"columns": status_meta(), "statuses": STATUSES})


def _short_task(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "status": t.status,
        "priority": t.priority,
        "size": t.size,
        "assignee": t.assignee,
        "external_blocker": t.external_blocker,
        "moved_at": t.moved_at,
        "blockers": t.blockers,
        "project_id": t.project_id,
        "archived": bool(t.archived_at),
        "type": t.type,
        "agent_model": t.agent_model,
        "origin": t.origin,
        "parent_id": t.parent_id,
        "url": _url(t.id),
    }


@mcp.tool()
def kanban_list(
    status: str | None = None,
    assignee: str | None = None,
    project_id: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """List tasks with optional filters.

    Args:
        status: one of backlog/analysis/approved/in_progress/testing/results/done/blocked/cancelled,
                or None for all.
        assignee: claude / agent:<name> / user, or None for all.
        project_id: project slug (see kanban_projects); default = this agent's
                    project (KANBAN_PROJECT_ID), "*" = all projects.
        include_archived: also list archived tasks (hidden from the board).

    Returns:
        {"ok": true, "data": {"tasks": [{...short fields}], "count": N}}
    """
    try:
        if status is not None:
            status = normalize_status(status)
        tasks = _get_store().list_tasks(
            status=status, assignee=assignee, project_id=_scope(project_id),
            include_archived=include_archived,
        )
    except Exception as e:
        return _err(str(e))
    return _ok({
        "tasks": [_short_task(t) for t in tasks],
        "count": len(tasks),
    })


@mcp.tool()
def kanban_projects() -> dict[str, Any]:
    """List projects with task counts per column.

    Call this at the start of a session to learn which projects exist.

    Returns:
        {"ok": true, "data": {"projects": [
            {"id": "myproj", "name": "My project", "color": "#0061A4",
             "path": "/abs/path", "task_counts": {"backlog": 5, ...},
             "total_tasks": 33, "archived": false}
        ]}}
    """
    try:
        projects = _get_store().list_projects(include_archived=False)
    except Exception as e:
        return _err(str(e))
    return _ok({"projects": [p.to_public() for p in projects]})


@mcp.tool()
def kanban_board(project_id: str | None = None, per_column: int = 5) -> dict[str, Any]:
    """Compact overview of a project board: counts + the first tasks of each column.

    Perfect for a quick "what's in progress?" answer without enumerating
    all 100+ tasks.

    Args:
        project_id: project slug (see kanban_projects); default = KANBAN_PROJECT_ID.
        per_column: how many tasks to show per column (default 5).
    """
    project_id = _scope(project_id)
    if not project_id:
        return _err("project_id is required (or set KANBAN_PROJECT_ID)")
    try:
        proj = _get_store().get_project(project_id)
        if proj is None:
            return _err(f"project {project_id} not found")
        tasks = _get_store().list_tasks(project_id=project_id)
    except Exception as e:
        return _err(str(e))
    by_status: dict[str, list[dict[str, Any]]] = {s: [] for s in STATUSES}
    for t in tasks:
        by_status[t.status].append(_short_task(t))
    summary = {
        "project": {"id": proj.id, "name": proj.name, "path": proj.path},
        "total": len(tasks),
        "by_status": {
            s: {
                "count": len(by_status[s]),
                "first_5": by_status[s][:max(0, per_column)],
            }
            for s in STATUSES if by_status[s]
        },
    }
    return _ok(summary)


@mcp.tool()
def kanban_search(query: str, project_id: str | None = None) -> dict[str, Any]:
    """Search tasks by substring in title/description (case-insensitive).

    Args:
        query: search term; rejected if shorter than 2 characters.
        project_id: default = KANBAN_PROJECT_ID; "*" = all projects.
    """
    if not query or len(query.strip()) < 2:
        return _err("query must be at least 2 characters")
    q = query.strip().lower()
    try:
        tasks = _get_store().list_tasks(project_id=_scope(project_id))
    except Exception as e:
        return _err(str(e))
    hits = [t for t in tasks if q in t.title.lower() or q in (t.description or "").lower()]
    return _ok({
        "tasks": [_short_task(t) for t in hits],
        "count": len(hits),
        "query": query,
    })


@mcp.tool()
def kanban_my_active(
    assignee: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Active tasks for the given assignee — in in_progress/testing.

    Perfect as the first query of a session: "what am I working on right now?"

    Args:
        assignee: defaults to this agent (KANBAN_ACTOR, else "claude");
                  or agent:<name>, user, ...
        project_id: default = KANBAN_PROJECT_ID; "*" = all projects.
    """
    who = _actor(assignee)
    try:
        tasks = _get_store().list_tasks(assignee=who, project_id=_scope(project_id))
    except Exception as e:
        return _err(str(e))
    active = [t for t in tasks if t.status in ("in_progress", "testing")]
    return _ok({
        "tasks": [_short_task(t) for t in active],
        "count": len(active),
        "assignee": who,
    })


@mcp.tool()
def kanban_get(task_id: str, history_limit: int = DEFAULT_HISTORY_LIMIT) -> dict[str, Any]:
    """Full task card: description, acceptance, links, blockers and the
    newest ``history_limit`` history rows (``history_total`` tells how many
    exist; use kanban_history for older ones)."""
    try:
        t = _get_store().get_task(task_id, history_limit=max(0, history_limit))
    except Exception as e:
        return _err(str(e))
    if not t:
        return _err(f"task {task_id} not found")
    return _ok(t.to_public())


@mcp.tool()
def kanban_history(task_id: str, limit: int = 50, before_id: int | None = None) -> dict[str, Any]:
    """One page of a task's history, oldest→newest within the page.

    Args:
        limit: rows per page (max 1000).
        before_id: pass ``next_before_id`` from the previous page to go further back.
    """
    try:
        items, total = _get_store().history(task_id, limit=limit, before_id=before_id)
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({
        "items": [asdict(h) for h in items],
        "total": total,
        "next_before_id": items[0].id if items and len(items) == max(1, min(limit, 1000)) else None,
    })


@mcp.tool()
def kanban_pull(task_id: str, assignee: str | None = None) -> dict[str, Any]:
    """Atomically "claim a task" from Approved → In progress.

    Conditions: the task must be in status ``approved`` AND its assignee
    must be either None or equal to ``assignee`` (default: this agent,
    KANBAN_ACTOR or "claude"). If another agent has already taken it, you
    get an error — try a different task. project_id is taken from the task
    itself, so you do not need to provide it.
    """
    try:
        _get_store().pull_task(task_id, assignee=_actor(assignee), session_id=_session_id())
        # The claim is when the agent starts work: hand back the full card.
        t = _get_store().get_task(task_id, history_limit=10)
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok(t.to_public() if t else {"id": task_id})


@mcp.tool()
def kanban_move(
    task_id: str,
    to_status: str,
    comment: str | None = None,
    expected_from: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Move a task to a new status.

    Args:
        task_id: T-XXX
        to_status: backlog / analysis / approved / in_progress / testing /
                   results / done / blocked / cancelled (see kanban_columns).
                   Finished work goes to "results" for the human's review.
        comment: optional comment, recorded in history.
        expected_from: only move if the task is still in this status — protects
                       against acting on a card a human or another agent has
                       just moved.
        actor: defaults to KANBAN_ACTOR or "claude".
    """
    try:
        to_status = normalize_status(to_status)
    except ValueError as e:
        return _err(str(e))
    denied = _check_agent_may_use(to_status)
    if denied:
        return _err(denied)
    try:
        t = _get_store().move_task(
            task_id, to_status, actor=_actor(actor), comment=comment,
            expected_from=expected_from,
        )
    except KeyError:
        return _err(f"task {task_id} not found")
    except StatusConflict as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))
    return _ok(_short_task(t))


@mcp.tool()
def kanban_assign(
    task_id: str,
    assignee: str | None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Set the task's assignee (e.g. "claude", "agent:reviewer", "user");
    null or "" clears it. Use this to hand a card to another agent or lane."""
    try:
        t = _get_store().assign_task(task_id, assignee, actor=_actor(actor))
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok(_short_task(t))


@mcp.tool()
def kanban_comment(
    task_id: str,
    text: str,
    actor: str | None = None,
) -> dict[str, Any]:
    """Add a comment to the task's history.

    Useful for recording the plan once you claim the task, or for
    capturing a test result.
    """
    try:
        _get_store().add_comment(task_id, text, actor=_actor(actor))
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({"task_id": task_id, "comment_added": True})


@mcp.tool()
def kanban_create(
    title: str,
    description: str = "",
    acceptance: str = "",
    status: str = "backlog",
    priority: str = "normal",
    size: str = "M",
    external_blocker: str | None = None,
    actor: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Create a new card. Defaults to Backlog; for immediate work pass status='in_progress'.

    To suggest follow-up work or a user story for the human to approve,
    prefer task_propose.

    Args:
        title: a short single-line title.
        description: markdown with the details.
        acceptance: acceptance criteria (what counts as "done").
        status: backlog (default) / approved / in_progress / ... (see kanban_columns).
        priority: critical / high / normal / low.
        size: S (<30 min) / M (<2 h) / L (>2 h) / XL.
        project_id: project slug; None = default (KANBAN_PROJECT_ID env,
                    else KANBAN_DEFAULT_PROJECT_ID). An unknown slug is an error.
    """
    try:
        status = normalize_status(status)
    except ValueError as e:
        return _err(str(e))
    denied = _check_agent_may_use(status)
    if denied:
        return _err(denied)
    pid = project_id or os.environ.get("KANBAN_PROJECT_ID") or DEFAULT_PROJECT_ID
    try:
        t = _get_store().create_task(
            title=title,
            description=description,
            acceptance=acceptance,
            status=status,
            priority=priority,
            size=size,
            external_blocker=external_blocker,
            actor=_actor(actor),
            project_id=pid,
            origin="agent",
            origin_session_id=_session_id(),
        )
    except Exception as e:
        return _err(str(e))
    return _ok(_short_task(t))


@mcp.tool()
def kanban_link(task_id: str, link_type: str, value: str) -> dict[str, Any]:
    """Attach a link to a task.

    Args:
        link_type: memory | file | pr | url | plan
        value: file name or URL.
    """
    if link_type not in LINK_TYPES:
        return _err(f"unknown link_type: {link_type}; valid: {list(LINK_TYPES)}")
    try:
        added = _get_store().add_link(task_id, link_type, value)
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({"task_id": task_id, "link": {"type": link_type, "value": value}, "added": added})


@mcp.tool()
def kanban_blockers(
    task_id: str, blocker_ids: list[str], actor: str | None = None
) -> dict[str, Any]:
    """Replace the list of internal blockers (dependencies): ``task_id``
    waits for every task in ``blocker_ids``. Unknown ids and cycles are
    rejected. Pass [] to clear."""
    try:
        stored = _get_store().set_blockers(task_id, blocker_ids, actor=_actor(actor))
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({"task_id": task_id, "blockers": stored})


@mcp.tool()
def kanban_update(
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    acceptance: str | None = None,
    priority: str | None = None,
    size: str | None = None,
    external_blocker: str | None = None,
    story_as_a: str | None = None,
    story_i_want: str | None = None,
    story_so_that: str | None = None,
    parent_id: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Update card fields (status: kanban_move; assignee: kanban_assign).

    Only the fields you pass are changed; external_blocker="" and
    parent_id="" clear them. The worker model is chosen in Analysis, not here.
    """
    try:
        t = _get_store().update_fields(
            task_id,
            actor=_actor(actor),
            title=title,
            description=description,
            acceptance=acceptance,
            priority=priority,
            size=size,
            external_blocker=external_blocker,
            story_as_a=story_as_a,
            story_i_want=story_i_want,
            story_so_that=story_so_that,
            parent_id=parent_id,
        )
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok(_short_task(t))


@mcp.tool()
def task_propose(
    title: str,
    description: str = "",
    type: str = "task",
    story_as_a: str = "",
    story_i_want: str = "",
    story_so_that: str = "",
    acceptance: str = "",
    parent_id: str | None = None,
    from_task_id: str | None = None,
    priority: str = "normal",
    size: str = "M",
    project_id: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Propose new work for the human to review: a follow-up you found, or
    a user story broken into tasks. The card always lands in Backlog, marked
    as proposed by you; only the human moves it on (Analysis → Approved).

    Args:
        title: a short single-line title.
        description: markdown with the details and why it matters.
        type: "task" (default) or "story".
        story_as_a / story_i_want / story_so_that: the user story, for
            type="story" ("As a <role>, I want <goal>, so that <benefit>").
        acceptance: acceptance criteria, ideally a "- [ ] ..." checklist.
        parent_id: the story this task belongs to.
        from_task_id: the card you were working on when you found this
            (default: this session's current card).
        priority: critical / high / normal / low.
        size: S / M / L / XL.
        project_id: default = KANBAN_PROJECT_ID, else the source card's project.
    """
    who = _actor(actor)
    store = _get_store()
    origin_task = (from_task_id or "").strip() or None
    sid = _session_id()
    if origin_task is None and sid:
        s = store.get_session(sid)
        origin_task = s.current_task_id if s else None
    pid = project_id or os.environ.get("KANBAN_PROJECT_ID")
    if not pid and origin_task:
        src = store.get_task(origin_task, history_limit=0)
        pid = src.project_id if src else None
    try:
        t = store.create_task(
            title=title,
            description=description,
            acceptance=acceptance,
            status="backlog",
            priority=priority,
            size=size,
            actor=who,
            project_id=pid or DEFAULT_PROJECT_ID,
            type=type,
            story_as_a=story_as_a,
            story_i_want=story_i_want,
            story_so_that=story_so_that,
            parent_id=parent_id,
            origin="agent",
            origin_session_id=sid,
            origin_task_id=origin_task,
        )
    except Exception as e:
        return _err(str(e))
    return _ok(_short_task(t))


@mcp.tool()
def session_register(
    agent_name: str | None = None,
    repo: str | None = None,
    project_id: str | None = None,
    model: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Announce this agent session on the board's sessions strip.

    Usually not needed: under Claude Code the session registers itself on
    its first change. Call it to set a clearer name (e.g. "analyst-1") or
    when your client does not provide a session id.

    Args:
        agent_name: name shown on the board (default KANBAN_ACTOR or "claude").
        repo: working directory or repository of the session.
        project_id: default = KANBAN_PROJECT_ID.
        model: opus / sonnet / haiku.
        task_id: the card you are working on, if any.
        session_id: default = Claude Code's session id; a new id is made up
            when there is none.
    """
    import uuid

    sid = (session_id or "").strip() or _session_id() or f"s-{uuid.uuid4().hex[:12]}"
    d = _session_defaults()
    try:
        s = _get_store().register_session(
            sid,
            (agent_name or "").strip() or d["agent_name"],
            machine=d["machine"],
            repo=(repo if repo is not None else d["repo"]),
            project_id=project_id or d["project_id"],
            model=model or d["model"],
            started_by=d["started_by"],
            current_task_id=task_id,
        )
    except Exception as e:
        return _err(str(e))
    _session.update(id=sid, registered=True)
    return _ok(s.to_public())


@mcp.tool()
def session_status(
    status: str | None = None,
    task_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Tell the board what this session is doing.

    Args:
        status: active / idle / waiting_input / finished.
        task_id: the card you are on now ("" = none).
        note: one short line, e.g. "running the replay tests".
    """
    _heartbeat()
    sid = _session_id()
    if not sid:
        return _err("no session: call session_register first")
    try:
        s = _get_store().touch_session(sid, status=status, task_id=task_id, note=note)
    except KeyError:
        return _err("session not registered: call session_register first")
    except Exception as e:
        return _err(str(e))
    return _ok(s.to_public())


@mcp.tool()
def inbox_get(wait_seconds: int = 0) -> dict[str, Any]:
    """Read the human's messages to you (instructions, comments on your
    reports, answers to your questions). Messages are marked read.

    Every tool response carries ``inbox_pending`` when something is waiting,
    so call this whenever you see it — and before you stop.

    Args:
        wait_seconds: 0 (default) returns at once; up to 60 waits for a
            message to arrive (use it after asking a question).
    """
    import time

    _heartbeat()
    sid = _session_id()
    if not sid:
        return _err("no session: call session_register first")
    deadline = time.monotonic() + max(0, min(int(wait_seconds or 0), 60))
    while True:
        try:
            msgs = _get_store().inbox(sid)
        except Exception as e:
            return _err(str(e))
        if msgs or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    return _ok({
        "messages": [
            {"id": m.id, "kind": m.kind, "text": m.body, "from": m.author,
             "task_id": m.task_id, "about": m.about, "report_id": m.report_id,
             "asset_id": m.asset_id, "sent_at": m.created_at}
            for m in msgs
        ],
        "count": len(msgs),
    })


@mcp.tool()
def inbox_reply(input_id: int, text: str, done: bool = True, actor: str | None = None) -> dict[str, Any]:
    """Answer one of the human's messages. The reply is threaded under it
    and appears in the card's history.

    Args:
        input_id: the message ``id`` from inbox_get.
        text: your answer — what you will do, or did.
        done: true (default) marks the message handled.
    """
    try:
        r = _get_store().reply_input(input_id, text, author=_actor(actor), done=done)
    except KeyError:
        return _err(f"message {input_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({"reply_id": r.id, "task_id": r.task_id})


def _resolve_path(p: str) -> str:
    """Agent paths are relative to the agent's working directory, not this
    server's (Claude Code starts MCP servers in the kanban repo)."""
    p = os.path.expanduser(p)
    if os.path.isabs(p):
        return p
    base = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD") or os.getcwd()
    return os.path.join(base, p)


def _asset_spec(a: Any, i: int) -> dict[str, Any]:
    import base64
    import binascii

    if not isinstance(a, dict):
        raise ValueError(f"assets[{i}] must be an object with a 'type'")
    unknown = set(a) - {"type", "caption", "path", "source", "figure", "data_base64", "filename"}
    if unknown:
        raise ValueError(f"assets[{i}]: unknown key(s) {sorted(unknown)}; use type, caption and "
                         "one of path / source / figure / data_base64")
    spec: dict[str, Any] = {"type": a.get("type"), "caption": a.get("caption") or "",
                            "filename": a.get("filename")}
    if a.get("figure") is not None:
        spec["figure"] = a["figure"]
    if a.get("source") is not None:
        spec["text"] = str(a["source"])
    if a.get("data_base64"):
        try:
            spec["data"] = base64.b64decode(a["data_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"assets[{i}].data_base64 is not valid base64") from None
    if a.get("path"):
        path = _resolve_path(str(a["path"]))
        if path.lower().endswith((".html", ".htm")) and str(a.get("type")).lower() == "plotly":
            raise ValueError(f"assets[{i}]: post the Plotly figure as JSON (fig.write_json / "
                             "fig.to_json()), not HTML")
        try:
            with open(path, "rb") as fh:
                spec["data"] = fh.read()
        except OSError as e:
            raise ValueError(f"assets[{i}].path: cannot read {path}: {e.strerror}") from None
        spec["filename"] = spec.get("filename") or os.path.basename(path)
    if not any(k in spec for k in ("figure", "text", "data")):
        raise ValueError(f"assets[{i}] needs a body: path, source, figure or data_base64")
    return spec


@mcp.tool()
def report_post(
    title: str,
    summary_md: str = "",
    task_id: str | None = None,
    final: bool = False,
    assets: list[dict[str, Any]] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Post a report on a card: a progress update, or (final=true) your
    result, which moves the card to Results for the human's review.

    Args:
        title: one line — the finding, e.g. "Heading drift halved with online bias".
        summary_md: Markdown: what you did, how you verified it, open issues.
            ```mermaid fences in it are drawn as diagrams.
        task_id: default = the card this session holds.
        final: true for the result of the card (moves it to Results).
        assets: figures, in order. Each is {"type", "caption", <body>} where
            type is one of:
              "mermaid"  — body "source": Mermaid text (flowchart, sequence, gantt, …)
              "plotly"   — body "figure": a Plotly figure dict ({"data": [...],
                           "layout": {...}}), or "path" to fig.write_json() output
              "snapshot" — an image (PNG/JPEG/GIF/WebP: screenshots, saved plots):
                           body "path" (absolute is safest) or "data_base64"
              "svg"      — a generic SVG drawing: body "path" or "source"
              "file"     — anything else to download: "path" or "data_base64"
            The human can comment on each figure; comments reach you via inbox_get.
    """
    who = _actor(actor)
    store = _get_store()
    sid = _session_id()
    tid = (task_id or "").strip() or None
    if tid is None and sid:
        sess = store.get_session(sid)
        tid = sess.current_task_id if sess else None
    if tid is None:
        return _err("task_id is required (this session holds no card)")
    try:
        specs = [_asset_spec(a, i) for i, a in enumerate(assets or [])]
    except ValueError as e:
        return _err(str(e))
    if final:
        denied = _check_agent_may_use("results")
        if denied:
            return _err(denied)
    try:
        r = store.create_report(tid, title, author=who, kind="result" if final else "progress",
                                summary_md=summary_md, session_id=sid, assets=specs)
        moved = False
        if final:
            t = store.get_task(tid, history_limit=0)
            if t and t.status != "results":
                store.move_task(tid, "results", actor=who, comment=f"result: {title}")
                moved = True
    except KeyError:
        return _err(f"task {tid} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({
        "report_id": r.id,
        "task_id": tid,
        "kind": r.kind,
        "assets": [{"id": a.id, "type": a.type, "caption": a.caption} for a in r.assets],
        "moved_to_results": moved,
        "url": _url(tid),
    })


@mcp.tool()
def analysis_submit(
    task_id: str,
    analysis_md: str,
    agent_model: str,
    model_rationale: str = "",
    acceptance: str | None = None,
    story_as_a: str | None = None,
    story_i_want: str | None = None,
    story_so_that: str | None = None,
    subtasks: list[dict[str, Any]] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Hand back your analysis of a card (you are the analyst agent). The
    card stays in Analysis; the human approves it with your model or another.

    Args:
        task_id: the card you analysed.
        analysis_md: scope, approach, risks, files involved — this becomes
            the worker agent's brief.
        agent_model: the worker model you recommend: "opus" (hard, cross-
            cutting, ambiguous), "sonnet" (most work) or "haiku" (small,
            mechanical, well specified).
        model_rationale: one line on why.
        acceptance: refined acceptance criteria, a "- [ ] ..." checklist.
        story_as_a / story_i_want / story_so_that: refined user story.
        subtasks: optional split, each {"title", "description"?, "acceptance"?,
            "size"?, "agent_model"?}; created in Analysis as children of the card.
    """
    try:
        t, created = _get_store().submit_analysis(
            task_id, actor=_actor(actor), analysis_md=analysis_md, agent_model=agent_model,
            model_rationale=model_rationale, acceptance=acceptance, story_as_a=story_as_a,
            story_i_want=story_i_want, story_so_that=story_so_that, subtasks=subtasks,
        )
    except KeyError:
        return _err(f"task {task_id} not found")
    except Exception as e:
        return _err(str(e))
    return _ok({"task": _short_task(t), "subtasks": [_short_task(c) for c in created]})


# ---------------------------------------------------------------------------
# Strict arguments
# ---------------------------------------------------------------------------

# Wrong names agents actually used, → the parameter they meant.
_ARG_HINTS: dict[str, tuple[str, ...]] = {
    "project": ("project_id",),
    "column": ("to_status", "status"),
    "status": ("to_status",),
    "body": ("description", "text"),
    "content": ("text", "description"),
    "message": ("text", "comment"),
    "comment": ("text",),
    "id": ("task_id",),
    "task": ("task_id",),
    "type": ("link_type",),
    "blockers": ("blocker_ids",),
    "limit": ("history_limit",),
}


def _strict_arg_model(model: Any, tool_name: str) -> Any:
    allowed = set(model.model_fields)

    def _reject_unknown(cls: Any, data: Any) -> Any:
        if isinstance(data, dict):
            unknown = [k for k in data if k not in allowed]
            if unknown:
                parts = []
                for k in unknown:
                    guess = next((h for h in _ARG_HINTS.get(k, ()) if h in allowed), None)
                    if guess is None:
                        close = difflib.get_close_matches(k, sorted(allowed), n=1)
                        guess = close[0] if close else None
                    parts.append(f"'{k}'" + (f" (did you mean '{guess}'?)" if guess else ""))
                raise ValueError(
                    f"unknown argument {', '.join(parts)}; {tool_name} accepts: "
                    + ", ".join(sorted(allowed))
                )
        return data

    namespace = {
        "__module__": model.__module__,
        "model_config": ConfigDict(arbitrary_types_allowed=True, extra="forbid"),
        "_reject_unknown": model_validator(mode="before")(classmethod(_reject_unknown)),
    }
    return type(model.__name__, (model,), namespace)


def _make_arguments_strict(server: FastMCP) -> None:
    """Unknown argument names are rejected instead of silently ignored.

    FastMCP's argument models ignore extra keys by default, so an agent that
    wrote ``project=`` / ``column=`` / ``body=`` created empty cards in the
    default project without noticing. Uses FastMCP internals; if they change,
    the server keeps working without the check.
    """
    try:
        for tool in server._tool_manager.list_tools():
            meta = tool.fn_metadata
            meta.arg_model = _strict_arg_model(meta.arg_model, tool.name)
            tool.parameters["additionalProperties"] = False
    except Exception:  # noqa: BLE001
        log.warning("could not enable strict tool arguments", exc_info=True)


_make_arguments_strict(mcp)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    from kanban_mcp import channel

    if channel.enabled():
        import anyio

        anyio.run(lambda: channel.serve(mcp, get_store=_get_store, session=_session,
                                        heartbeat=_heartbeat))
        return
    mcp.run()


if __name__ == "__main__":
    main()
