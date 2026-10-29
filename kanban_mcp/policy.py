"""Guard rails that apply to AI agents, whichever transport they use
(stdio MCP in ``server.py``, HTTP MCP bridged onto the REST API in
``kanban_ui/main.py``)."""
from __future__ import annotations

import os

from kanban_store.store import normalize_status

# Mission Board default: Analysis and Approved are the human's gates (an
# agent never approves its own work or its own proposals), and closing a card
# is the human's call after review. Agents finish by moving a card to Results.
DEFAULT_HUMAN_ONLY = "analysis,approved,done"


def human_only() -> set[str]:
    """Statuses agents may not move or create cards into
    (``KANBAN_MCP_HUMAN_ONLY``, default ``analysis,approved,done``)."""
    raw = os.environ.get("KANBAN_MCP_HUMAN_ONLY", DEFAULT_HUMAN_ONLY)
    out: set[str] = set()
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        try:  # pre-v6 names (uat) and friendly spellings map to column ids
            out.add(normalize_status(x))
        except ValueError:
            out.add(x)
    return out


def check_agent_may_use(status: str) -> str | None:
    """Error text if agents may not put cards into ``status``, else None."""
    reserved = human_only()
    if status in reserved:
        return (
            f"'{status}' is reserved for a human (KANBAN_MCP_HUMAN_ONLY="
            f"{','.join(sorted(reserved))}). Finished work goes to 'results' with a "
            "comment describing what was done and how it was verified; new work "
            "goes to the backlog (task_propose); the human approves and closes."
        )
    return None
