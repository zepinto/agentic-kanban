"""Kanban — SQLite store.

Single source of truth: ``tasks.db`` next to the repo root (gitignored), or
the path from env ``KANBAN_DB``.

Public API:
    Store.list_tasks(status=..., assignee=..., project_id=..., include_archived=False)
    Store.get_task(task_id, history_limit=None)
    Store.history(task_id, limit=100, before_id=None)
    Store.create_task(title, status="backlog", ...)
    Store.move_task(task_id, to_status, actor, comment=None)
    Store.assign_task(task_id, assignee, actor)
    Store.archive_task(task_id, archived=True, actor)
    Store.add_comment(task_id, text, actor)
    Store.add_link(task_id, type, value)
    Store.set_blockers(task_id, blocker_ids)
    Store.snapshot()  -> dict (for JSON snapshots)

Every mutation that changes something writes one row to ``task_history``.
Calls that would not change anything (same priority, same assignee, move
into the column the task is already in) write nothing — the history is the
event log that automation and webhooks consume, so no-op rows are noise.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

DEFAULT_PROJECT_ID = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .reports import ReportsMixin

# ============================================================================
# Status model
# ============================================================================

# 9 columns in left-to-right UI order (Mission Board workflow):
#   backlog → analysis → approved → in_progress → (testing) → results → done
# plus blocked / cancelled. ``analysis`` is where a card is refined (by the
# human or an analyst agent) and gets its worker model; ``results`` is the
# human review gate an agent reaches by posting its final result. ``testing``
# is kept for projects that want it but is hidden in the UI by default.
# Schema v6 renamed the old ``analyst`` (agent claimed the card) to
# ``in_progress`` and ``uat`` to ``results``.
STATUSES: list[str] = [
    "backlog",
    "analysis",
    "approved",
    "in_progress",
    "testing",
    "results",
    "done",
    "blocked",
    "cancelled",
]

# Worker models a card can be assigned to (Claude Code ``--model`` aliases).
AGENT_MODELS: tuple[str, ...] = ("opus", "sonnet", "haiku")
DEFAULT_AGENT_MODEL = os.environ.get("KANBAN_DEFAULT_MODEL", "sonnet").strip().lower() or "sonnet"

# Card types: a plain task, or a user story (as a / I want / so that).
TASK_TYPES: tuple[str, ...] = ("task", "story")

# Who created the card. Agent proposals always land in the backlog.
ORIGINS: tuple[str, ...] = ("user", "agent")

# States an agent session reports about itself. "offline" is never stored:
# it is derived from a stale heartbeat (see Store.list_sessions).
SESSION_STATUSES: tuple[str, ...] = ("active", "idle", "waiting_input", "finished")

# Link types accepted by add_link / create_task(links=...).
# ``plan`` — a planning document (e.g. a Claude Code plan-mode file).
LINK_TYPES: tuple[str, ...] = ("memory", "file", "pr", "url", "plan")

# Statuses in which a blocker counts as resolved.
RESOLVED_STATUSES: tuple[str, ...] = ("done", "cancelled")

PRIORITIES: tuple[str, ...] = ("critical", "high", "normal", "low")
SIZES: tuple[str, ...] = ("S", "M", "L", "XL")

# Spellings agents and people actually use for columns → canonical id.
_STATUS_ALIASES: dict[str, str] = {
    "todo": "backlog", "бэклог": "backlog",
    "согласовано": "approved",
    "analyst": "analysis", "analytics": "analysis", "аналитика": "analysis",
    "refine": "analysis", "refinement": "analysis",
    "inprogress": "in_progress", "wip": "in_progress", "doing": "in_progress",
    "в_работе": "in_progress",
    "qa": "testing", "тестирование": "testing",
    # pre-v6 name of the review column, kept so old rules and scripts work
    "uat": "results", "result": "results", "review": "results",
    "acceptance": "results", "приёмка": "results", "приемка": "results",
    "closed": "done", "закрыто": "done",
    "заблокировано": "blocked",
    "canceled": "cancelled", "отменено": "cancelled",
}


def normalize_status(value: str) -> str:
    """``"In progress"``, ``"in-progress"``, ``"Testing"``, ``"В работе"`` → canonical id.

    Raises ``ValueError`` listing the valid ids for anything unrecognised.
    """
    raw = str(value if value is not None else "").strip()
    key = raw.lower().replace("-", "_").replace(" ", "_")
    if key in STATUSES:
        return key
    if key in _STATUS_ALIASES:
        return _STATUS_ALIASES[key]
    if key.replace("_", "") in _STATUS_ALIASES:
        return _STATUS_ALIASES[key.replace("_", "")]
    raise ValueError(f"unknown status: {value!r}; valid: {', '.join(STATUSES)}")


def normalize_priority(value: str) -> str:
    key = str(value if value is not None else "").strip().lower()
    if key == "medium":
        return "normal"
    if key in PRIORITIES:
        return key
    raise ValueError(f"unknown priority: {value!r}; valid: {', '.join(PRIORITIES)}")


def normalize_size(value: str) -> str:
    key = str(value if value is not None else "").strip().upper()
    if key in SIZES:
        return key
    raise ValueError(f"unknown size: {value!r}; valid: {', '.join(SIZES)}")


def normalize_choice(value: str | None, choices: tuple[str, ...], what: str) -> str:
    key = str(value if value is not None else "").strip().lower()
    if key in choices:
        return key
    raise ValueError(f"unknown {what}: {value!r}; valid: {', '.join(choices)}")


def normalize_model(value: str | None) -> str | None:
    """``"Opus"`` → ``"opus"``; ``""``/``None`` → ``None`` (no model set).

    Full model ids are accepted by family name (``claude-sonnet-5-5`` →
    ``sonnet``) so a card stays readable on the board.
    """
    raw = str(value if value is not None else "").strip().lower()
    if not raw:
        return None
    if raw in AGENT_MODELS:
        return raw
    for m in AGENT_MODELS:
        if m in raw:
            return m
    raise ValueError(f"unknown agent model: {value!r}; valid: {', '.join(AGENT_MODELS)}")


def status_meta() -> list[dict[str, Any]]:
    """Column metadata for the UI and for agents (``kanban_columns``).

    ``owner`` is who normally moves cards into the column; ``hidden``
    columns are kept but collapsed out of the default board.
    """
    return [
        {"id": "backlog",     "title": "Backlog",     "owner": "any",
         "hint": "new entries from you, and tasks or stories proposed by agents"},
        {"id": "analysis",    "title": "Analysis",    "owner": "user",
         "hint": "refine scope, story and acceptance criteria; pick the worker model "
                 "(you, or an analyst agent)"},
        {"id": "approved",    "title": "Approved",    "owner": "user",
         "hint": "queue: an agent on the card's model is started for each card here"},
        {"id": "in_progress", "title": "In progress", "owner": "agent",
         "hint": "an agent is working on the card"},
        {"id": "testing",     "title": "Testing",     "owner": "agent", "hidden": True,
         "hint": "optional verification step"},
        {"id": "results",     "title": "Results",     "owner": "agent",
         "hint": "the agent's final result, waiting for your review"},
        {"id": "done",        "title": "Done",        "owner": "user",
         "hint": "accepted"},
        {"id": "blocked",     "title": "Blocked",     "owner": "any",
         "hint": "waiting on a question or a dependency"},
        {"id": "cancelled",   "title": "Cancelled",   "owner": "user",
         "hint": "dropped"},
    ]


# ============================================================================
# Models
# ============================================================================


@dataclass
class TaskHistory:
    id: int
    task_id: str
    ts: str
    actor: str
    action: str
    from_status: str | None
    to_status: str | None
    comment: str | None


@dataclass
class Task:
    id: str
    title: str
    status: str
    priority: str
    size: str
    assignee: str | None
    description: str
    acceptance: str
    external_blocker: str | None
    created_at: str
    moved_at: str
    column_order: int
    project_id: str = DEFAULT_PROJECT_ID
    archived_at: str | None = None
    # --- Mission Board (schema v6) ---
    type: str = "task"                       # task | story
    story_as_a: str = ""
    story_i_want: str = ""
    story_so_that: str = ""
    parent_id: str | None = None             # a story's tasks point at it
    origin: str = "user"                     # user | agent
    origin_session_id: str | None = None     # agent session that proposed it
    origin_task_id: str | None = None        # card the agent was working on
    agent_model: str | None = None           # opus | sonnet | haiku
    analysis_md: str = ""                    # analysis notes for the agent's brief
    analysed_by: str | None = None           # user / agent session that did the analysis
    model_rationale: str = ""                # why this model (analyst agent)
    assigned_session_id: str | None = None   # worker session bound to the card
    run_count: int = 0
    links: list[dict[str, str]] = field(default_factory=list)
    history: list[TaskHistory] = field(default_factory=list)
    # Total number of history rows; ``history`` may hold only the newest ones.
    history_total: int = 0
    blockers: list[str] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        d = asdict(self)
        d["history"] = [asdict(h) for h in self.history]
        return d


@dataclass
class Project:
    id: str
    name: str
    color: str
    icon: str
    sort_order: int
    archived: bool
    created_at: str
    path: str | None = None
    default_model: str = DEFAULT_AGENT_MODEL  # prefills a card's worker model
    analyst_model: str = DEFAULT_AGENT_MODEL  # model the analyst agent runs on
    task_counts: dict[str, int] = field(default_factory=dict)
    total_tasks: int = 0

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Session:
    """An agent session (a Claude Code process) known to the board."""
    id: str
    agent_name: str
    machine: str
    repo: str
    project_id: str | None
    model: str | None
    status: str                    # active / idle / waiting_input / finished / offline
    current_task_id: str | None
    note: str
    started_by: str                # hub (started by the board) | manual
    started_at: str
    last_heartbeat: str
    pending_inputs: int = 0        # messages the agent has not read yet

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


# Kinds of message in an agent's inbox.
INPUT_KINDS: tuple[str, ...] = ("instruction", "comment", "feedback", "answer", "stop", "reply")


@dataclass
class Input:
    """A message to (or a reply from) an agent session."""
    id: int
    session_id: str | None
    task_id: str | None
    kind: str
    body: str
    author: str
    parent_id: int | None
    created_at: str
    delivered_at: str | None
    acked_at: str | None
    agent_name: str | None = None   # recipient's display name, when known
    report_id: int | None = None    # the report this message comments on
    asset_id: int | None = None     # the figure in it
    about: str | None = None        # human-readable anchor: report title / figure caption

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


# ============================================================================
# Store
# ============================================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StatusConflict(RuntimeError):
    """The task is not in the status the caller expected (see move_task)."""


# Actors that are software, not a person. Moving a card out of Blocked resets
# run_command's max_runs budget only when a person does it — otherwise a
# "retry blocked cards" rule would defeat the limit it is meant to respect.
MACHINE_ACTORS = {"automation", "claude", "codex", "plan-import", "maintenance", "inbox"}


def is_machine_actor(actor: str | None) -> bool:
    a = (actor or "").strip().lower()
    return a in MACHINE_ACTORS or a.startswith(("agent:", "claude", "codex"))


def _default_db_path() -> Path:
    env = os.environ.get("KANBAN_DB", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    return Path(__file__).resolve().parent.parent / "tasks.db"


def normalize_links(links: Iterable[Any] | None) -> list[tuple[str, str]]:
    """Validate ``[{"type": ..., "value": ...}]`` and return (type, value) pairs.

    ``link_type`` is accepted as an alias of ``type`` (the MCP tool uses that
    name). Raises ``ValueError`` with a readable message instead of letting a
    malformed payload surface as ``KeyError``.
    """
    out: list[tuple[str, str]] = []
    for i, ln in enumerate(links or []):
        if not isinstance(ln, dict):
            raise ValueError(f"links[{i}] must be an object with 'type' and 'value'")
        type_ = ln.get("type", ln.get("link_type"))
        value = ln.get("value")
        if not isinstance(type_, str) or type_ not in LINK_TYPES:
            raise ValueError(
                f"links[{i}].type must be one of {list(LINK_TYPES)}, got {type_!r}"
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"links[{i}].value must be a non-empty string")
        out.append((type_, value.strip()))
    return out


class Store(ReportsMixin):
    """Thread-safe wrapper around SQLite. One instance per process."""

    _lock = threading.RLock()

    def __init__(self, db_path: str | Path | None = None, *, create: bool = True):
        """``create=False`` refuses to start a new, empty database: used by
        the MCP server, where a wrong ``KANBAN_DB`` would otherwise give the
        agent an empty board of its own instead of an error."""
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        if not create and not self.db_path.exists():
            raise FileNotFoundError(
                f"kanban database not found: {self.db_path} — check KANBAN_DB "
                "(the web server creates the database on its first start)"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # report assets live next to the database unless KANBAN_ASSETS_DIR says otherwise
        assets_env = os.environ.get("KANBAN_ASSETS_DIR", "").strip()
        self.assets_dir = (Path(os.path.expanduser(assets_env)) if assets_env
                           else self.db_path.parent / "kanban_assets")
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; explicit transactions use BEGIN
            # The UI server, every MCP process and helper scripts share one
            # file — wait for a concurrent writer instead of failing fast.
            timeout=15.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        with self._lock:
            self._conn.executescript(schema)
            self._migrate_v2()
            self._migrate_v3()
            self._migrate_v4()
            self._migrate_v5()
            self._migrate_v6()

    def _schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 1

    # Columns added to tasks / projects in v6, with their SQL definitions.
    _V6_TASK_COLUMNS: tuple[tuple[str, str], ...] = (
        ("type", "TEXT NOT NULL DEFAULT 'task'"),
        ("story_as_a", "TEXT NOT NULL DEFAULT ''"),
        ("story_i_want", "TEXT NOT NULL DEFAULT ''"),
        ("story_so_that", "TEXT NOT NULL DEFAULT ''"),
        ("parent_id", "TEXT"),
        ("origin", "TEXT NOT NULL DEFAULT 'user'"),
        ("origin_session_id", "TEXT"),
        ("origin_task_id", "TEXT"),
        ("agent_model", "TEXT"),
        ("analysis_md", "TEXT NOT NULL DEFAULT ''"),
        ("analysed_by", "TEXT"),
        ("model_rationale", "TEXT NOT NULL DEFAULT ''"),
        ("assigned_session_id", "TEXT"),
        ("run_count", "INTEGER NOT NULL DEFAULT 0"),
    )
    _V6_PROJECT_COLUMNS: tuple[tuple[str, str], ...] = (
        ("default_model", f"TEXT NOT NULL DEFAULT '{DEFAULT_AGENT_MODEL}'"),
        ("analyst_model", f"TEXT NOT NULL DEFAULT '{DEFAULT_AGENT_MODEL}'"),
    )

    def _migrate_v6(self) -> None:
        """v5 → v6 (Mission Board): story / analysis / model fields on tasks,
        model defaults on projects, the sessions table (schema.sql), and the
        new column model — ``analyst`` cards (claimed by an agent) become
        ``in_progress``, ``uat`` becomes ``results``."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        for name, ddl in self._V6_TASK_COLUMNS:
            if name not in cols:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
        pcols = {r[1] for r in self._conn.execute("PRAGMA table_info(projects)").fetchall()}
        for name, ddl in self._V6_PROJECT_COLUMNS:
            if name not in pcols:
                self._conn.execute(f"ALTER TABLE projects ADD COLUMN {name} {ddl}")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)")
        icols = {r[1] for r in self._conn.execute("PRAGMA table_info(inputs)").fetchall()}
        for name in ("report_id", "asset_id"):
            if name not in icols:
                self._conn.execute(f"ALTER TABLE inputs ADD COLUMN {name} INTEGER")
        if self._schema_version() < 6:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("UPDATE tasks SET status='in_progress' WHERE status='analyst'")
                self._conn.execute("UPDATE tasks SET status='results' WHERE status='uat'")
                self._conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _migrate_v5(self) -> None:
        """v4 → v5: tasks.archived_at (rule_firings comes from schema.sql)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "archived_at" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")
        if self._schema_version() < 5:
            self._conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")

    def _migrate_v4(self) -> None:
        """v3 → v4: project_sources table (created via schema.sql,
        this method only bumps the version)."""
        if self._schema_version() < 4:
            self._conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")

    def _migrate_v3(self) -> None:
        """v2 → v3: projects.path TEXT (Claude Code project directory)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "path" not in cols:
            self._conn.execute("ALTER TABLE projects ADD COLUMN path TEXT")
        if self._schema_version() < 3:
            self._conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")

    def _migrate_v2(self) -> None:
        """v1 → v2: adds tasks.project_id for existing databases and
        creates a default project (id/name are configurable via env).

        Idempotent: checks PRAGMA table_info before running ALTER.
        """
        version = self._schema_version()
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "project_id" not in cols:
            # old pre-v2 database — add the column with a default
            default_id = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
            self._conn.execute(
                f"ALTER TABLE tasks ADD COLUMN project_id TEXT NOT NULL DEFAULT '{default_id}'"
            )
        # The project_id index is always created (idempotent). For a fresh
        # database the column appeared from CREATE TABLE in schema.sql; for
        # older databases it is created after the ALTER above.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_project_status "
            "ON tasks(project_id, status, column_order)"
        )
        # The default project is created ONLY when the database has no
        # projects at all (fresh install). Existing databases keep their
        # own projects without an extra "default" being added on top.
        row = self._conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        if row["n"] == 0:
            default_id = os.environ.get("KANBAN_DEFAULT_PROJECT_ID", "default")
            default_name = os.environ.get("KANBAN_DEFAULT_PROJECT_NAME", "Default")
            default_color = os.environ.get("KANBAN_DEFAULT_PROJECT_COLOR", "#0061A4")
            default_icon = os.environ.get(
                "KANBAN_DEFAULT_PROJECT_ICON", default_name[:1].upper()
            )
            self._conn.execute(
                "INSERT INTO projects (id, name, color, icon, sort_order, archived, created_at) "
                "VALUES (?, ?, ?, ?, 0, 0, ?)",
                (default_id, default_name, default_color, default_icon, _now()),
            )
        if version < 2:
            self._conn.execute(
                "UPDATE meta SET value='2' WHERE key='schema_version'"
            )

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='next_id'"
            ).fetchone()
            n = int(row["value"]) if row else 1
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='next_id'", (str(n + 1),)
            )
            return f"T-{n:03d}"

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_tasks(
        self,
        status: str | Iterable[str] | None = None,
        assignee: str | None = None,
        project_id: str | None = None,
        *,
        include_archived: bool = False,
    ) -> list[Task]:
        """List of tasks with filters, sorted by (status, column_order).

        ``project_id=None`` means "all projects". The board UI always
        passes a concrete project_id. Archived tasks are skipped unless
        ``include_archived=True``.
        """
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            if isinstance(status, str):
                statuses = [status]
            else:
                statuses = list(status)
            placeholders = ",".join("?" * len(statuses))
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        if assignee is not None:
            sql += " AND assignee = ?"
            params.append(assignee)
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        if not include_archived:
            sql += " AND archived_at IS NULL"
        sql += " ORDER BY status, column_order, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            tasks = [self._row_to_task(r) for r in rows]
            self._attach_links_and_blockers(tasks)
        return tasks

    def get_task(self, task_id: str, *, history_limit: int | None = None) -> Task | None:
        """Full card. ``history_limit`` keeps only the newest N history rows
        (``history_total`` still reports the full count)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not row:
                return None
            t = self._row_to_task(row)
            self._attach_links_and_blockers([t])
            t.history_total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM task_history WHERE task_id=?", (task_id,)
            ).fetchone()["n"]
            if history_limit is None:
                h_rows = self._conn.execute(
                    "SELECT * FROM task_history WHERE task_id=? ORDER BY id ASC",
                    (task_id,),
                ).fetchall()
            else:
                h_rows = self._conn.execute(
                    "SELECT * FROM task_history WHERE task_id=? ORDER BY id DESC LIMIT ?",
                    (task_id, max(0, int(history_limit))),
                ).fetchall()[::-1]
            t.history = [self._row_to_history(r) for r in h_rows]
        return t

    def history(
        self, task_id: str, *, limit: int = 100, before_id: int | None = None
    ) -> tuple[list[TaskHistory], int]:
        """One page of a task's history, oldest→newest within the page.

        Pages go backwards in time: pass the smallest ``id`` of the current
        page as ``before_id`` to get the previous page. Returns
        ``(items, total)``. Raises ``KeyError`` for an unknown task.
        """
        with self._lock:
            if not self._task_exists(task_id):
                raise KeyError(task_id)
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM task_history WHERE task_id=?", (task_id,)
            ).fetchone()["n"]
            sql = "SELECT * FROM task_history WHERE task_id=?"
            params: list[Any] = [task_id]
            if before_id is not None:
                sql += " AND id < ?"
                params.append(int(before_id))
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(max(1, min(int(limit), 1000)))
            rows = self._conn.execute(sql, params).fetchall()[::-1]
        return [self._row_to_history(r) for r in rows], total

    def history_since(self, after_id: int, *, limit: int = 500) -> list[TaskHistory]:
        """History rows with ``id > after_id`` in insertion order (event feed)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_history WHERE id > ? ORDER BY id ASC LIMIT ?",
                (int(after_id), int(limit)),
            ).fetchall()
        return [self._row_to_history(r) for r in rows]

    def max_history_id(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM task_history"
            ).fetchone()
        return int(row["m"])

    def board(self) -> dict[str, list[Task]]:
        """Group tasks by status, in column order."""
        result: dict[str, list[Task]] = {s: [] for s in STATUSES}
        for t in self.list_tasks():
            result.setdefault(t.status, []).append(t)
        return result

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create_task(
        self,
        title: str,
        *,
        status: str = "backlog",
        priority: str = "normal",
        size: str = "M",
        description: str = "",
        acceptance: str = "",
        assignee: str | None = None,
        external_blocker: str | None = None,
        actor: str = "user",
        links: list[dict[str, str]] | None = None,
        task_id: str | None = None,
        project_id: str = DEFAULT_PROJECT_ID,
        type: str = "task",
        story_as_a: str = "",
        story_i_want: str = "",
        story_so_that: str = "",
        parent_id: str | None = None,
        origin: str = "user",
        origin_session_id: str | None = None,
        origin_task_id: str | None = None,
        agent_model: str | None = None,
        analysis_md: str = "",
    ) -> Task:
        status = normalize_status(status)
        priority = normalize_priority(priority)
        size = normalize_size(size)
        type_ = normalize_choice(type, TASK_TYPES, "task type")
        origin = normalize_choice(origin, ORIGINS, "origin")
        agent_model = normalize_model(agent_model)
        parent_id = (parent_id or "").strip() or None
        origin_task_id = (origin_task_id or "").strip() or None
        if not title or not title.strip():
            raise ValueError("title must not be empty")
        link_pairs = normalize_links(links)
        ts = _now()
        with self._lock:
            if self.get_project(project_id) is None:
                known = ", ".join(p.id for p in self.list_projects(include_archived=True))
                raise ValueError(f"unknown project: {project_id} (known: {known})")
            for ref, what in ((parent_id, "parent"), (origin_task_id, "origin task")):
                if ref is not None and not self._task_exists(ref):
                    raise ValueError(f"unknown {what}: {ref}")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                tid = task_id or self._next_id()
                # column_order — last in the column + 1 (per project)
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                    "WHERE status=? AND project_id=?",
                    (status, project_id),
                ).fetchone()
                col_order = (row["m"] + 1) if row else 0
                self._conn.execute(
                    """
                    INSERT INTO tasks (id, title, status, priority, size, assignee,
                                        description, acceptance, external_blocker,
                                        created_at, moved_at, column_order, project_id,
                                        type, story_as_a, story_i_want, story_so_that,
                                        parent_id, origin, origin_session_id,
                                        origin_task_id, agent_model, analysis_md)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tid,
                        title.strip(),
                        status,
                        priority,
                        size,
                        assignee or None,
                        description,
                        acceptance,
                        external_blocker or None,
                        ts,
                        ts,
                        col_order,
                        project_id,
                        type_,
                        story_as_a.strip(),
                        story_i_want.strip(),
                        story_so_that.strip(),
                        parent_id,
                        origin,
                        (origin_session_id or "").strip() or None,
                        origin_task_id,
                        agent_model,
                        analysis_md,
                    ),
                )
                for type_, value in link_pairs:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO task_links (task_id, type, value) VALUES (?, ?, ?)",
                        (tid, type_, value),
                    )
                note = None
                if origin == "agent":
                    note = "proposed by an agent" + (
                        f" while working on {origin_task_id}" if origin_task_id else ""
                    )
                self._insert_history(tid, ts, actor, "create", None, status, note)
                if status == "approved":
                    self._ensure_agent_model(tid, project_id, ts, actor)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        task = self.get_task(tid)
        assert task is not None
        return task

    def move_task(
        self,
        task_id: str,
        to_status: str,
        *,
        actor: str = "user",
        comment: str | None = None,
        column_order: int | None = None,
        expected_from: str | None = None,
    ) -> Task:
        """Move a task to ``to_status``.

        Moving into the column the task is already in is not a status
        change: only the order within the column changes (when
        ``column_order`` is given), ``moved_at`` is left alone and the
        optional comment is recorded as a plain comment.

        ``expected_from`` makes the move conditional: if the task is no
        longer in that status (a human or another agent moved it), a
        ``StatusConflict`` is raised and nothing changes.
        """
        to_status = normalize_status(to_status)
        if expected_from is not None:
            expected_from = normalize_status(expected_from)
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT status, project_id FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                from_status = row["status"]
                project_id = row["project_id"]
                if expected_from is not None and from_status != expected_from:
                    raise StatusConflict(
                        f"task {task_id} is in '{from_status}', not '{expected_from}' — "
                        "someone else moved it; re-read the card before acting"
                    )
                if from_status == to_status:
                    if column_order is not None:
                        self._place_in_column(task_id, project_id, to_status, column_order)
                    if comment:
                        self._insert_history(task_id, ts, actor, "comment", None, None, comment)
                else:
                    self._conn.execute(
                        "UPDATE tasks SET status=?, moved_at=? WHERE id=?",
                        (to_status, ts, task_id),
                    )
                    if column_order is None:
                        # append to the end of the project's column
                        r2 = self._conn.execute(
                            "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                            "WHERE status=? AND project_id=? AND id != ?",
                            (to_status, project_id, task_id),
                        ).fetchone()
                        self._conn.execute(
                            "UPDATE tasks SET column_order=? WHERE id=?",
                            ((r2["m"] + 1) if r2 else 0, task_id),
                        )
                    else:
                        self._place_in_column(task_id, project_id, to_status, column_order)
                    self._insert_history(
                        task_id, ts, actor, "move", from_status, to_status, comment
                    )
                    if to_status == "approved":
                        self._ensure_agent_model(task_id, project_id, ts, actor)
                    if from_status == "blocked" and not is_machine_actor(actor):
                        # A person unblocking the card is the "try again"
                        # signal: fresh max_runs budget for run_command rules.
                        self._conn.execute(
                            "DELETE FROM command_runs WHERE task_id=?", (task_id,)
                        )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        task = self.get_task(task_id, history_limit=0)
        assert task is not None
        return task

    def _ensure_agent_model(self, task_id: str, project_id: str, ts: str, actor: str) -> None:
        """An approved card always names its worker model: if none was picked
        in Analysis, the project default fills it (and history says so).
        Runs inside the caller's transaction."""
        row = self._conn.execute(
            "SELECT agent_model FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None or row["agent_model"]:
            return
        prow = self._conn.execute(
            "SELECT default_model FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        model = normalize_model(prow["default_model"] if prow else None) or DEFAULT_AGENT_MODEL
        self._conn.execute("UPDATE tasks SET agent_model=? WHERE id=?", (model, task_id))
        self._insert_history(
            task_id, ts, actor, "update", None, None,
            f"agent_model: — → {model} (project default)",
        )

    def _place_in_column(
        self, task_id: str, project_id: str, status: str, index: int
    ) -> None:
        """Put ``task_id`` at ``index`` of its column and renumber the column
        0..n-1 (drag-drop sends the target index, not a free order value)."""
        ids = [
            r["id"]
            for r in self._conn.execute(
                "SELECT id FROM tasks WHERE project_id=? AND status=? AND id != ? "
                "AND archived_at IS NULL ORDER BY column_order, id",
                (project_id, status, task_id),
            ).fetchall()
        ]
        index = max(0, min(int(index), len(ids)))
        ids.insert(index, task_id)
        for i, tid in enumerate(ids):
            self._conn.execute("UPDATE tasks SET column_order=? WHERE id=?", (i, tid))

    def assign_task(self, task_id: str, assignee: str | None, *, actor: str) -> Task:
        """Set (or clear with ``None``/``""``) the assignee. No-op if unchanged."""
        assignee = (assignee or "").strip() or None
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT assignee FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                if row["assignee"] != assignee:
                    self._conn.execute(
                        "UPDATE tasks SET assignee=? WHERE id=?", (assignee, task_id)
                    )
                    self._insert_history(
                        task_id, ts, actor, "assign", None, None,
                        f"assignee → {assignee if assignee else '—'}",
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id, history_limit=0)
        assert t is not None
        return t

    def pull_task(
        self, task_id: str, assignee: str = "claude", *, session_id: str | None = None,
    ) -> Task:
        """Atomic: assignee IS NULL → assignee, status approved → in_progress.

        Used by Claude/an agent for a safe "claim the task" operation.
        ``session_id`` binds the card to the claiming agent session, so later
        comments on the card can be routed to it.
        """
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT assignee, status, project_id FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                if row["assignee"] is not None and row["assignee"] != assignee:
                    raise RuntimeError(
                        f"task {task_id} already assigned to {row['assignee']}"
                    )
                if row["status"] != "approved":
                    raise RuntimeError(
                        f"task {task_id} is in '{row['status']}', not 'approved'"
                    )
                # move to in_progress and claim (per project)
                r2 = self._conn.execute(
                    "SELECT COALESCE(MAX(column_order), -1) AS m FROM tasks "
                    "WHERE status='in_progress' AND project_id=?",
                    (row["project_id"],),
                ).fetchone()
                col_order = (r2["m"] + 1) if r2 else 0
                sid = (session_id or "").strip() or None
                self._conn.execute(
                    """UPDATE tasks SET status='in_progress', assignee=?, moved_at=?,
                              column_order=?, run_count=run_count+1,
                              assigned_session_id=COALESCE(?, assigned_session_id)
                       WHERE id=?""",
                    (assignee, ts, col_order, sid, task_id),
                )
                self._insert_history(
                    task_id, ts, assignee, "move", "approved", "in_progress", "pulled"
                )
                if sid:
                    self._conn.execute(
                        "UPDATE sessions SET current_task_id=?, status='active', "
                        "last_heartbeat=? WHERE id=?",
                        (task_id, ts, sid),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id, history_limit=0)
        assert t is not None
        return t

    def add_comment(self, task_id: str, text: str, *, actor: str) -> None:
        if not text or not text.strip():
            raise ValueError("comment text must not be empty")
        ts = _now()
        with self._lock:
            if not self._task_exists(task_id):
                raise KeyError(task_id)
            self._insert_history(task_id, ts, actor, "comment", None, None, text)

    def add_link(self, task_id: str, type_: str, value: str) -> bool:
        """Attach a link. Returns False if the same link already exists."""
        ((type_, value),) = normalize_links([{"type": type_, "value": value}])
        with self._lock:
            if not self._task_exists(task_id):
                raise KeyError(task_id)
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO task_links (task_id, type, value) VALUES (?, ?, ?)",
                (task_id, type_, value),
            )
            return cur.rowcount > 0

    def set_blockers(
        self, task_id: str, blocker_ids: list[str], *, actor: str = "user"
    ) -> list[str]:
        """Replace the task's internal blockers. Returns the stored list.

        Rejects unknown ids, self-references and cycles with ``ValueError``
        (``KeyError`` for an unknown ``task_id``).
        """
        wanted: list[str] = []
        for b in blocker_ids:
            b = (b or "").strip()
            if b and b not in wanted:
                wanted.append(b)
        ts = _now()
        with self._lock:
            if not self._task_exists(task_id):
                raise KeyError(task_id)
            if task_id in wanted:
                raise ValueError(f"{task_id} cannot block itself")
            unknown = [b for b in wanted if not self._task_exists(b)]
            if unknown:
                raise ValueError(f"unknown blocker task(s): {', '.join(unknown)}")
            cycle = self._find_cycle(task_id, wanted)
            if cycle:
                raise ValueError("blockers would create a cycle: " + " → ".join(cycle))
            current = sorted(
                r["blocker_id"]
                for r in self._conn.execute(
                    "SELECT blocker_id FROM task_blockers WHERE task_id=?", (task_id,)
                ).fetchall()
            )
            if current == sorted(wanted):
                return wanted
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM task_blockers WHERE task_id=?", (task_id,)
                )
                for b in wanted:
                    self._conn.execute(
                        "INSERT INTO task_blockers (task_id, blocker_id) VALUES (?, ?)",
                        (task_id, b),
                    )
                self._insert_history(
                    task_id, ts, actor, "update", None, None,
                    "blockers: " + (", ".join(wanted) if wanted else "—"),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return wanted

    def _find_cycle(self, task_id: str, new_blockers: list[str]) -> list[str] | None:
        """Path back to ``task_id`` through existing blocker edges, if any."""
        stack: list[tuple[str, list[str]]] = [(b, [task_id, b]) for b in new_blockers]
        seen: set[str] = set()
        while stack:
            node, path = stack.pop()
            if node == task_id:
                return path
            if node in seen:
                continue
            seen.add(node)
            for r in self._conn.execute(
                "SELECT blocker_id FROM task_blockers WHERE task_id=?", (node,)
            ).fetchall():
                stack.append((r["blocker_id"], path + [r["blocker_id"]]))
        return None

    # Fields whose old → new values are short enough to show in history.
    _SHOW_VALUES = ("priority", "size", "type", "agent_model")
    # Nullable text fields where "" means "clear" (stored as NULL).
    _NULLABLE = ("external_blocker", "agent_model", "parent_id", "analysed_by")

    def update_fields(
        self,
        task_id: str,
        *,
        actor: str,
        title: str | None = None,
        priority: str | None = None,
        size: str | None = None,
        description: str | None = None,
        acceptance: str | None = None,
        external_blocker: str | None = None,
        type: str | None = None,
        story_as_a: str | None = None,
        story_i_want: str | None = None,
        story_so_that: str | None = None,
        parent_id: str | None = None,
        agent_model: str | None = None,
        analysis_md: str | None = None,
        analysed_by: str | None = None,
        model_rationale: str | None = None,
    ) -> Task:
        """Update the given fields (``None`` = leave alone).

        Only fields whose value actually changes are written, and the
        history row lists just those (``priority: normal → high; title``).
        ``external_blocker=""`` clears the external blocker (likewise
        ``agent_model``, ``parent_id``, ``analysed_by``). A call that changes
        nothing writes nothing.
        """
        requested = {
            "title": title.strip() if isinstance(title, str) else None,
            "priority": priority,
            "size": size,
            "description": description,
            "acceptance": acceptance,
            "external_blocker": external_blocker,
            "type": type,
            "story_as_a": story_as_a,
            "story_i_want": story_i_want,
            "story_so_that": story_so_that,
            "parent_id": parent_id,
            "agent_model": agent_model,
            "analysis_md": analysis_md,
            "analysed_by": analysed_by,
            "model_rationale": model_rationale,
        }
        if requested["title"] == "":
            raise ValueError("title must not be empty")
        if requested["priority"] is not None:
            requested["priority"] = normalize_priority(requested["priority"])
        if requested["size"] is not None:
            requested["size"] = normalize_size(requested["size"])
        if requested["type"] is not None:
            requested["type"] = normalize_choice(requested["type"], TASK_TYPES, "task type")
        if requested["agent_model"] is not None:
            # "" clears; anything else must name a known model
            requested["agent_model"] = normalize_model(requested["agent_model"]) or ""
        pid = (requested["parent_id"] or "").strip()
        if pid:
            if pid == task_id:
                raise ValueError(f"{task_id} cannot be its own parent")
            with self._lock:
                if not self._task_exists(pid):
                    raise ValueError(f"unknown parent: {pid}")
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                sets: list[str] = []
                params: list[Any] = []
                notes: list[str] = []
                for col, val in requested.items():
                    if val is None:
                        continue
                    if col in self._NULLABLE:
                        val = val.strip() or None
                    if col in ("story_as_a", "story_i_want", "story_so_that"):
                        val = val.strip()
                    if row[col] == val:
                        continue
                    sets.append(f"{col} = ?")
                    params.append(val)
                    if col in self._SHOW_VALUES:
                        notes.append(f"{col}: {row[col]} → {val}")
                    else:
                        notes.append(col)
                if sets:
                    params.append(task_id)
                    self._conn.execute(
                        f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params
                    )
                    self._insert_history(
                        task_id, ts, actor, "update", None, None, "; ".join(notes)
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id, history_limit=0)
        assert t is not None
        return t

    def request_analysis(self, task_id: str, *, actor: str) -> tuple[Task, str]:
        """Hand a card to the analyst agent: move it to Analysis (from Backlog
        or Blocked) and record ``analysis_requested`` — the event a
        ``task_event`` rule turns into an analyst run. Returns the card and the
        project's analyst model."""
        t = self.get_task(task_id, history_limit=0)
        if t is None:
            raise KeyError(task_id)
        if t.status not in ("backlog", "analysis", "blocked"):
            raise ValueError(f"{task_id} is in '{t.status}': analysis is for cards before approval")
        if t.status != "analysis":
            self.move_task(task_id, "analysis", actor=actor, comment="sent to the analyst agent")
        proj = self.get_project(t.project_id)
        model = (proj.analyst_model if proj else None) or DEFAULT_AGENT_MODEL
        with self._lock:
            self._insert_history(task_id, _now(), actor, "analysis_requested", None, None,
                                 f"analyst model: {model}")
        t = self.get_task(task_id, history_limit=0)
        assert t is not None
        return t, model

    def submit_analysis(
        self,
        task_id: str,
        *,
        actor: str,
        analysis_md: str,
        agent_model: str,
        model_rationale: str = "",
        acceptance: str | None = None,
        story_as_a: str | None = None,
        story_i_want: str | None = None,
        story_so_that: str | None = None,
        subtasks: list[dict[str, Any]] | None = None,
    ) -> tuple[Task, list[Task]]:
        """What an analyst agent hands back: notes, criteria, the worker model
        and its reason, optional subtasks (created in Analysis as children).
        The card stays in Analysis for the human to approve."""
        if not (analysis_md or "").strip():
            raise ValueError("analysis_md must not be empty")
        if normalize_model(agent_model) is None:
            raise ValueError(f"agent_model must be one of {', '.join(AGENT_MODELS)}")
        parent = self.get_task(task_id, history_limit=0)
        if parent is None:
            raise KeyError(task_id)
        subs = list(subtasks or [])
        for i, st in enumerate(subs):
            if not isinstance(st, dict) or not str(st.get("title") or "").strip():
                raise ValueError(f"subtasks[{i}] needs a title")
        t = self.update_fields(
            task_id, actor=actor, analysis_md=analysis_md, agent_model=agent_model,
            model_rationale=model_rationale, acceptance=acceptance, story_as_a=story_as_a,
            story_i_want=story_i_want, story_so_that=story_so_that, analysed_by=actor,
        )
        created = []
        for st in subs:
            created.append(self.create_task(
                str(st["title"]), status="analysis", project_id=parent.project_id,
                description=str(st.get("description") or ""),
                acceptance=str(st.get("acceptance") or ""),
                size=str(st.get("size") or "M"), parent_id=task_id, origin="agent",
                origin_task_id=task_id, agent_model=st.get("agent_model") or None,
                actor=actor,
            ))
        return t, created

    def archive_task(
        self, task_id: str, *, archived: bool = True, actor: str = "user",
        comment: str | None = None,
    ) -> Task:
        """Hide a task from the board without changing its status.

        Archiving keeps ``done`` tasks ``done`` (instead of relabelling them
        ``cancelled``); ``archived=False`` brings the task back. No-op if the
        task is already in the requested state.
        """
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT archived_at FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise KeyError(task_id)
                is_archived = row["archived_at"] is not None
                if is_archived != archived:
                    self._conn.execute(
                        "UPDATE tasks SET archived_at=? WHERE id=?",
                        (ts if archived else None, task_id),
                    )
                    self._insert_history(
                        task_id, ts, actor, "archive" if archived else "unarchive",
                        None, None, comment,
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        t = self.get_task(task_id, history_limit=0)
        assert t is not None
        return t

    def reorder(self, task_id: str, new_order: int) -> None:
        """Change the order within the current column."""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET column_order=? WHERE id=?", (new_order, task_id)
            )

    # ------------------------------------------------------------------
    # Rule firings (idempotency for polling automation rules)
    # ------------------------------------------------------------------

    def rule_last_fired(self, rule_key: str, task_id: str, anchor: str) -> str | None:
        """When ``rule_key`` last acted on ``task_id`` in episode ``anchor``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fired_at FROM rule_firings WHERE rule_key=? AND task_id=? AND anchor=?",
                (rule_key, task_id, anchor),
            ).fetchone()
        return row["fired_at"] if row else None

    def record_rule_firing(self, rule_key: str, task_id: str, anchor: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO rule_firings (rule_key, task_id, anchor, fired_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(rule_key, task_id, anchor) DO UPDATE SET fired_at=excluded.fired_at""",
                (rule_key, task_id, anchor, _now()),
            )

    def command_runs(self, rule_key: str, task_id: str) -> int:
        """How many times run_command ``rule_key`` was started for ``task_id``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT runs FROM command_runs WHERE rule_key=? AND task_id=?",
                (rule_key, task_id),
            ).fetchone()
        return int(row["runs"]) if row else 0

    def bump_command_runs(self, rule_key: str, task_id: str, delta: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO command_runs (rule_key, task_id, runs, last_started_at)
                   VALUES (?, ?, MAX(0, ?), ?)
                   ON CONFLICT(rule_key, task_id) DO UPDATE
                   SET runs = MAX(0, runs + ?),
                       last_started_at = CASE WHEN ? > 0 THEN excluded.last_started_at
                                              ELSE last_started_at END""",
                (rule_key, task_id, delta, _now(), delta, delta),
            )

    def set_command_rc(self, rule_key: str, task_id: str, rc: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE command_runs SET last_rc=? WHERE rule_key=? AND task_id=?",
                (rc, rule_key, task_id),
            )

    def reset_command_runs(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM command_runs WHERE task_id=?", (task_id,))

    def save_command_job(
        self, rule_key: str, task_id: str, *, state: str, rule_name: str,
        ctx: dict[str, Any], pid: int | None = None, proc_start: str | None = None,
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO command_jobs
                   (rule_key, task_id, state, rule_name, ctx, pid, proc_start,
                    queued_at, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(rule_key, task_id) DO UPDATE SET
                     state=excluded.state, rule_name=excluded.rule_name, ctx=excluded.ctx,
                     pid=excluded.pid, proc_start=excluded.proc_start,
                     started_at=CASE WHEN excluded.state='running' THEN excluded.started_at
                                     ELSE command_jobs.started_at END""",
                (rule_key, task_id, state, rule_name,
                 json.dumps(ctx, ensure_ascii=False), pid, proc_start, now,
                 now if state == "running" else None),
            )

    def delete_command_job(self, rule_key: str, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM command_jobs WHERE rule_key=? AND task_id=?", (rule_key, task_id)
            )

    def list_command_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            # running first, then the queue in insertion order (rowid: the
            # upsert keeps it when a queued job starts)
            rows = self._conn.execute(
                "SELECT * FROM command_jobs "
                "ORDER BY CASE state WHEN 'running' THEN 0 ELSE 1 END, rowid"
            ).fetchall()
        return [{**dict(r), "ctx": json.loads(r["ctx"])} for r in rows]

    # ------------------------------------------------------------------
    # Agent sessions
    # ------------------------------------------------------------------

    # A session that has not been heard from for this long shows as offline.
    SESSION_OFFLINE_AFTER_S = int(os.environ.get("KANBAN_SESSION_OFFLINE_AFTER", "900"))

    def register_session(
        self,
        session_id: str,
        agent_name: str,
        *,
        machine: str = "",
        repo: str = "",
        project_id: str | None = None,
        model: str | None = None,
        started_by: str = "manual",
        current_task_id: str | None = None,
    ) -> Session:
        """Create or refresh a session. Re-registering an existing id (a
        resumed Claude Code session) keeps its ``started_at``."""
        session_id = (session_id or "").strip()
        agent_name = (agent_name or "").strip()
        if not session_id:
            raise ValueError("session_id must not be empty")
        if not agent_name:
            raise ValueError("agent_name must not be empty")
        if started_by not in ("hub", "manual"):
            raise ValueError("started_by must be 'hub' or 'manual'")
        model = normalize_model(model)
        project_id = (project_id or "").strip() or None
        current_task_id = (current_task_id or "").strip() or None
        ts = _now()
        with self._lock:
            if project_id is not None and self.get_project(project_id) is None:
                raise ValueError(f"unknown project: {project_id}")
            if current_task_id is not None and not self._task_exists(current_task_id):
                raise ValueError(f"unknown task: {current_task_id}")
            self._conn.execute(
                """INSERT INTO sessions (id, agent_name, machine, repo, project_id, model,
                                         status, current_task_id, note, started_by,
                                         started_at, last_heartbeat)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, '', ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     agent_name=excluded.agent_name, machine=excluded.machine,
                     repo=excluded.repo,
                     project_id=COALESCE(excluded.project_id, sessions.project_id),
                     model=COALESCE(excluded.model, sessions.model),
                     current_task_id=COALESCE(excluded.current_task_id, sessions.current_task_id),
                     status='active', last_heartbeat=excluded.last_heartbeat""",
                (session_id, agent_name, machine.strip(), repo.strip(), project_id, model,
                 current_task_id, started_by, ts, ts),
            )
        s = self.get_session(session_id)
        assert s is not None
        return s

    def touch_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        task_id: str | None = None,
        note: str | None = None,
    ) -> Session:
        """Heartbeat, optionally with a new status / current card / note.
        ``task_id=""`` clears the current card. Raises ``KeyError`` for an
        unknown session."""
        sets = ["last_heartbeat = ?"]
        params: list[Any] = [_now()]
        if status is not None:
            sets.append("status = ?")
            params.append(normalize_choice(status, SESSION_STATUSES, "session status"))
        if note is not None:
            sets.append("note = ?")
            params.append(note.strip()[:500])
        with self._lock:
            if task_id is not None:
                tid = task_id.strip() or None
                if tid is not None and not self._task_exists(tid):
                    raise ValueError(f"unknown task: {tid}")
                sets.append("current_task_id = ?")
                params.append(tid)
            params.append(session_id)
            cur = self._conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params
            )
            if cur.rowcount == 0:
                raise KeyError(session_id)
        s = self.get_session(session_id)
        assert s is not None
        return s

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        include_finished: bool = False,
        max_age_s: int | None = 86400,
    ) -> list[Session]:
        """Sessions, most recently heard from first. Sessions silent for more
        than ``SESSION_OFFLINE_AFTER_S`` report status ``offline``; those
        silent longer than ``max_age_s`` (default a day) are left out."""
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        if not include_finished:
            sql += " AND status != 'finished'"
        if max_age_s is not None:
            cutoff = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() - max_age_s, timezone.utc
            ).isoformat(timespec="seconds")
            sql += " AND last_heartbeat >= ?"
            params.append(cutoff)
        sql += " ORDER BY last_heartbeat DESC, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            sessions = [self._row_to_session(r) for r in rows]
            for s in sessions:
                s.pending_inputs = self._pending_count(s.id)
        return sessions

    # ------------------------------------------------------------------
    # Messages to agents (inbox)
    # ------------------------------------------------------------------

    def session_for_task(self, task_id: str) -> str | None:
        """The session a message about ``task_id`` should reach: the agent
        bound to the card, else a live session currently on it, else the
        agent that proposed it. None when no agent is attached."""
        with self._lock:
            row = self._conn.execute(
                "SELECT assigned_session_id, origin_session_id FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["assigned_session_id"]:
                return row["assigned_session_id"]
            r2 = self._conn.execute(
                "SELECT id FROM sessions WHERE current_task_id=? AND status != 'finished' "
                "ORDER BY last_heartbeat DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if r2:
                return r2["id"]
            return row["origin_session_id"]

    def send_input(
        self,
        body: str,
        *,
        author: str,
        session_id: str | None = None,
        task_id: str | None = None,
        kind: str = "instruction",
        parent_id: int | None = None,
        report_id: int | None = None,
        asset_id: int | None = None,
    ) -> Input:
        """Queue a message for an agent. Addressed to a session, a card (the
        card's agent, resolved now — or later, by whoever claims it), or both.
        ``report_id`` / ``asset_id`` anchor it to a report or one figure in it
        (the card and the posting agent are taken from the report). A message
        about a card is also written to the card's history."""
        body = (body or "").strip()
        if not body:
            raise ValueError("message text must not be empty")
        kind = normalize_choice(kind, INPUT_KINDS, "message kind")
        session_id = (session_id or "").strip() or None
        task_id = (task_id or "").strip() or None
        if asset_id is not None:
            asset = self.get_asset(asset_id)
            if asset is None:
                raise KeyError(f"asset {asset_id}")
            report_id = asset.report_id
        if report_id is not None:
            rep_ = self.get_report(report_id)
            if rep_ is None:
                raise KeyError(f"report {report_id}")
            task_id = task_id or rep_.task_id
            if session_id is None and rep_.session_id and self.get_session(rep_.session_id):
                session_id = rep_.session_id
            if kind == "instruction":
                kind = "comment"
        if session_id is None and task_id is None:
            raise ValueError("a message needs a session_id or a task_id")
        ts = _now()
        with self._lock:
            if task_id is not None and not self._task_exists(task_id):
                raise KeyError(task_id)
            if session_id is not None and self.get_session(session_id) is None:
                raise KeyError(session_id)
            if session_id is None:
                session_id = self.session_for_task(task_id)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """INSERT INTO inputs (session_id, task_id, kind, body, author,
                                           parent_id, report_id, asset_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, task_id, kind, body, author or "user", parent_id,
                     report_id, asset_id, ts),
                )
                iid = cur.lastrowid
                if task_id is not None:
                    who = self._agent_name(session_id)
                    about = self._input_about(report_id, asset_id)
                    lead = f"to {who}" if who else "(waiting for an agent)"
                    note = f"{lead}{f' on {about}' if about else ''}: {body}"
                    self._insert_history(task_id, ts, author or "user", "message", None, None, note)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        msg = self.get_input(iid)
        assert msg is not None
        return msg

    def reply_input(self, input_id: int, body: str, *, author: str, done: bool = True) -> Input:
        """An agent answers a message. The reply is threaded under it and
        lands in the card's history; ``done`` marks the message handled."""
        body = (body or "").strip()
        if not body:
            raise ValueError("reply text must not be empty")
        parent = self.get_input(input_id)
        if parent is None:
            raise KeyError(input_id)
        ts = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """INSERT INTO inputs (session_id, task_id, kind, body, author,
                                           parent_id, created_at, delivered_at)
                       VALUES (?, ?, 'reply', ?, ?, ?, ?, ?)""",
                    (parent.session_id, parent.task_id, body, author, parent.id, ts, ts),
                )
                rid = cur.lastrowid
                self._conn.execute(
                    "UPDATE inputs SET delivered_at=COALESCE(delivered_at, ?)"
                    + (", acked_at=?" if done else "") + " WHERE id=?",
                    (ts, ts, parent.id) if done else (ts, parent.id),
                )
                if parent.task_id:
                    self._insert_history(parent.task_id, ts, author, "reply", None, None, body)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        r = self.get_input(rid)
        assert r is not None
        return r

    def inbox(self, session_id: str, *, mark_delivered: bool = True, limit: int = 50) -> list[Input]:
        """Unread messages for a session, oldest first: those addressed to it,
        plus card messages still waiting for an agent on a card it holds (they
        are claimed for this session as they are read)."""
        ts = _now()
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM inputs
                    WHERE delivered_at IS NULL AND kind != 'reply' AND (
                      session_id = ?
                      OR (session_id IS NULL AND task_id IN ({self._held_tasks_sql()}))
                    )
                    ORDER BY id LIMIT ?""",
                (session_id, session_id, session_id, int(limit)),
            ).fetchall()
            msgs = [self._row_to_input(r) for r in rows]
            if mark_delivered and msgs:
                ids = [m.id for m in msgs]
                ph = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE inputs SET delivered_at=?, session_id=COALESCE(session_id, ?) "
                    f"WHERE id IN ({ph})",
                    (ts, session_id, *ids),
                )
                for m in msgs:
                    m.delivered_at = ts
                    m.session_id = m.session_id or session_id
        return msgs

    @staticmethod
    def _held_tasks_sql() -> str:
        """Subquery (two ? = session id) for the cards a session holds."""
        return ("SELECT id FROM tasks WHERE assigned_session_id = ? "
                "UNION SELECT current_task_id FROM sessions WHERE id = ? "
                "AND current_task_id IS NOT NULL")

    def _pending_count(self, session_id: str) -> int:
        row = self._conn.execute(
            f"""SELECT COUNT(*) AS n FROM inputs
                WHERE delivered_at IS NULL AND kind != 'reply' AND (
                  session_id = ?
                  OR (session_id IS NULL AND task_id IN ({self._held_tasks_sql()})))""",
            (session_id, session_id, session_id),
        ).fetchone()
        return int(row["n"])

    def pending_inputs(self, session_id: str) -> int:
        with self._lock:
            return self._pending_count(session_id)

    def get_input(self, input_id: int) -> Input | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM inputs WHERE id=?", (int(input_id),)).fetchone()
        return self._row_to_input(row) if row else None

    def list_inputs(
        self, *, task_id: str | None = None, session_id: str | None = None, limit: int = 200,
    ) -> list[Input]:
        """Messages and replies for a card or a session, oldest first."""
        sql = "SELECT * FROM inputs WHERE 1=1"
        params: list[Any] = []
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()[::-1]
        return [self._row_to_input(r) for r in rows]

    def _agent_name(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        row = self._conn.execute(
            "SELECT agent_name FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return row["agent_name"] if row else None

    def _row_to_input(self, row: sqlite3.Row) -> Input:
        return Input(
            id=row["id"],
            session_id=row["session_id"],
            task_id=row["task_id"],
            kind=row["kind"],
            body=row["body"],
            author=row["author"],
            parent_id=row["parent_id"],
            created_at=row["created_at"],
            delivered_at=row["delivered_at"],
            acked_at=row["acked_at"],
            agent_name=self._agent_name(row["session_id"]),
            report_id=row["report_id"] if "report_id" in row.keys() else None,
            asset_id=row["asset_id"] if "asset_id" in row.keys() else None,
            about=self._input_about(
                row["report_id"] if "report_id" in row.keys() else None,
                row["asset_id"] if "asset_id" in row.keys() else None,
            ),
        )

    def _input_about(self, report_id: int | None, asset_id: int | None) -> str | None:
        """'figure “Bias vs time” in “Drift study”' / 'report “Drift study”'."""
        if report_id is None and asset_id is None:
            return None
        if asset_id is not None:
            row = self._conn.execute(
                "SELECT a.caption, a.type, a.position, r.title FROM report_assets a "
                "JOIN reports r ON r.id = a.report_id WHERE a.id=?", (asset_id,),
            ).fetchone()
            if row:
                name = row["caption"] or f"{row['type']} #{row['position'] + 1}"
                return f"figure “{name}” in report “{row['title']}”"
        if report_id is not None:
            row = self._conn.execute("SELECT title FROM reports WHERE id=?", (report_id,)).fetchone()
            if row:
                return f"report “{row['title']}”"
        return None

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        status = row["status"]
        if status != "finished":
            try:
                age = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(row["last_heartbeat"])
                ).total_seconds()
            except ValueError:
                age = 0
            if age > self.SESSION_OFFLINE_AFTER_S:
                status = "offline"
        return Session(
            id=row["id"],
            agent_name=row["agent_name"],
            machine=row["machine"],
            repo=row["repo"],
            project_id=row["project_id"],
            model=row["model"],
            status=status,
            current_task_id=row["current_task_id"],
            note=row["note"],
            started_by=row["started_by"],
            started_at=row["started_at"],
            last_heartbeat=row["last_heartbeat"],
        )

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        """All projects with task_counts aggregated by status (archived
        tasks are not counted)."""
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY sort_order, name"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
            counts_rows = self._conn.execute(
                "SELECT project_id, status, COUNT(*) AS n "
                "FROM tasks WHERE archived_at IS NULL GROUP BY project_id, status"
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {}
        for r in counts_rows:
            counts.setdefault(r["project_id"], {})[r["status"]] = r["n"]
            totals[r["project_id"]] = totals.get(r["project_id"], 0) + r["n"]
        result = []
        for r in rows:
            p = self._row_to_project(r)
            p.task_counts = counts.get(p.id, {})
            p.total_tasks = totals.get(p.id, 0)
            result.append(p)
        return result

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_project(row)

    def create_project(
        self,
        project_id: str,
        name: str,
        *,
        color: str = "#0061A4",
        icon: str = "",
        sort_order: int | None = None,
        path: str | None = None,
        default_model: str | None = None,
        analyst_model: str | None = None,
    ) -> Project:
        ts = _now()
        default_model = normalize_model(default_model) or DEFAULT_AGENT_MODEL
        analyst_model = normalize_model(analyst_model) or DEFAULT_AGENT_MODEL
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if sort_order is None:
                    r = self._conn.execute(
                        "SELECT COALESCE(MAX(sort_order), -1) AS m FROM projects"
                    ).fetchone()
                    sort_order = (r["m"] + 1) if r else 0
                self._conn.execute(
                    """INSERT INTO projects
                       (id, name, color, icon, sort_order, archived, path, created_at,
                        default_model, analyst_model)
                       VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                    (project_id, name, color, icon or name[:1].upper(), sort_order, path, ts,
                     default_model, analyst_model),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        p = self.get_project(project_id)
        assert p is not None
        return p

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        color: str | None = None,
        icon: str | None = None,
        sort_order: int | None = None,
        path: str | None = None,
        default_model: str | None = None,
        analyst_model: str | None = None,
    ) -> Project:
        sets: list[str] = []
        params: list[Any] = []
        if default_model is not None:
            default_model = normalize_model(default_model) or DEFAULT_AGENT_MODEL
        if analyst_model is not None:
            analyst_model = normalize_model(analyst_model) or DEFAULT_AGENT_MODEL
        # path is forwarded as-is (None means "leave alone", "" means "clear").
        for col, val in (
            ("name", name), ("color", color), ("icon", icon),
            ("sort_order", sort_order), ("path", path),
            ("default_model", default_model), ("analyst_model", analyst_model),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                # an empty string for path becomes NULL in the database
                params.append(None if (col == "path" and val == "") else val)
        if not sets:
            p = self.get_project(project_id)
            if p is None:
                raise KeyError(project_id)
            return p
        params.append(project_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row:
                raise KeyError(project_id)
            self._conn.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE id=?", params
            )
        p = self.get_project(project_id)
        assert p is not None
        return p

    # ------------------------------------------------------------------
    # Project sources (one source per project)
    # ------------------------------------------------------------------

    def set_project_source(
        self, project_id: str, type_: str, config: dict[str, Any]
    ) -> None:
        ts = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO project_sources (project_id, type, config, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE
                   SET type=excluded.type, config=excluded.config""",
                (project_id, type_, json.dumps(config, ensure_ascii=False), ts),
            )

    def get_project_source(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_sources WHERE project_id=?", (project_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "project_id": row["project_id"],
            "type": row["type"],
            "config": json.loads(row["config"]),
            "last_sync_at": row["last_sync_at"],
            "created_at": row["created_at"],
        }

    def update_source_sync_time(self, project_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE project_sources SET last_sync_at=? WHERE project_id=?",
                (_now(), project_id),
            )

    def delete_project_source(self, project_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM project_sources WHERE project_id=?", (project_id,)
            )

    def archive_project(self, project_id: str, archived: bool = True) -> Project:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not row:
                raise KeyError(project_id)
            self._conn.execute(
                "UPDATE projects SET archived=? WHERE id=?",
                (1 if archived else 0, project_id),
            )
        p = self.get_project(project_id)
        assert p is not None
        return p

    def _row_to_project(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            color=row["color"],
            icon=row["icon"],
            sort_order=row["sort_order"],
            archived=bool(row["archived"]),
            created_at=row["created_at"],
            path=row["path"] if "path" in row.keys() else None,
            default_model=(row["default_model"] if "default_model" in row.keys() else None)
            or DEFAULT_AGENT_MODEL,
            analyst_model=(row["analyst_model"] if "analyst_model" in row.keys() else None)
            or DEFAULT_AGENT_MODEL,
        )

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Dump the whole board as a plain dict for JSON persistence."""
        tasks = self.list_tasks(include_archived=True)
        projects = self.list_projects(include_archived=True)
        return {
            "exported_at": _now(),
            "schema_version": 6,
            "projects": [p.to_public() for p in projects],
            "tasks": [t.to_public() for t in tasks],
        }

    def save_snapshot(self, dest_dir: str | Path | None = None) -> Path:
        if dest_dir is None:
            dest_dir = Path(__file__).resolve().parent.parent / "snapshots"
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fp = dest_dir / f"{date_part}.json"
        fp.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return fp

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _task_exists(self, task_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM tasks WHERE id=?", (task_id,)
        ).fetchone() is not None

    def _insert_history(
        self,
        task_id: str,
        ts: str,
        actor: str,
        action: str,
        from_status: str | None,
        to_status: str | None,
        comment: str | None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO task_history
               (task_id, ts, actor, action, from_status, to_status, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, ts, actor or "user", action, from_status, to_status, comment),
        )

    def _attach_links_and_blockers(self, tasks: list[Task]) -> None:
        """Load links and blockers for many tasks with two queries."""
        if not tasks:
            return
        by_id = {t.id: t for t in tasks}
        ids = list(by_id)
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start:chunk_start + 500]
            ph = ",".join("?" * len(chunk))
            for r in self._conn.execute(
                f"SELECT task_id, type, value FROM task_links WHERE task_id IN ({ph}) "
                "ORDER BY type, value",
                chunk,
            ).fetchall():
                by_id[r["task_id"]].links.append({"type": r["type"], "value": r["value"]})
            for r in self._conn.execute(
                f"SELECT task_id, blocker_id FROM task_blockers WHERE task_id IN ({ph}) "
                "ORDER BY blocker_id",
                chunk,
            ).fetchall():
                by_id[r["task_id"]].blockers.append(r["blocker_id"])

    @staticmethod
    def _row_to_history(r: sqlite3.Row) -> TaskHistory:
        return TaskHistory(
            id=r["id"],
            task_id=r["task_id"],
            ts=r["ts"],
            actor=r["actor"],
            action=r["action"],
            from_status=r["from_status"],
            to_status=r["to_status"],
            comment=r["comment"],
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        keys = row.keys()
        return Task(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            priority=row["priority"],
            size=row["size"],
            assignee=row["assignee"],
            description=row["description"],
            acceptance=row["acceptance"],
            external_blocker=row["external_blocker"],
            created_at=row["created_at"],
            moved_at=row["moved_at"],
            column_order=row["column_order"],
            project_id=row["project_id"] if "project_id" in keys else DEFAULT_PROJECT_ID,
            archived_at=row["archived_at"] if "archived_at" in keys else None,
            **{
                name: row[name]
                for name, _ in Store._V6_TASK_COLUMNS
                if name in keys and row[name] is not None
            },
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
