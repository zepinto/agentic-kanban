# Agent launcher

Scripts the kanban invokes via a `run_command` action when something happens
to a card. The goal: **"approve a card → an agent picks it up on the card's
model and drives it to Results with a report"** — and when it doesn't, the card
says why instead of sitting in In progress forever. The same script also runs
the **analyst** (*Analyse with agent*) and **resumes** an agent when you message
a card whose agent has stopped.

## What's in here

| File | What it does |
|---|---|
| [`launch-claude.sh`](launch-claude.sh) | `launch-claude.sh <task_id> <project_id> [work\|analyse\|resume]` — runs Claude Code (`claude -p`) headless in the project directory, with the kanban MCP server passed explicitly, the message Stop hook, a hard timeout, and a safety net that blocks the card with a reason if the agent leaves it behind. |

## How to wire it up

1. **Bind the kanban project to the code directory** — UI → `⋯` next to the
   project → `Project directory` (e.g. `~/code/myapp`). Optionally run
   `python -m kanban_mcp.connect ~/code/myapp --project myproj` so
   interactive Claude sessions there get the kanban tools too (the launcher
   itself doesn't need it).

2. **Add a rule in `kanban_data/rules.json`**:
   ```json
   {
     "rules": [
       {
         "name": "Claude on approved",
         "trigger": {"type": "task_moved", "to_status": "approved", "project_id": "myproj"},
         "action": {
           "type": "run_command",
           "cmd": "/abs/path/to/agent-kanban/examples/agent-launcher/launch-claude.sh",
           "args": ["{task_id}", "{project_id}"],
           "log_file": "~/Library/Logs/agent-kanban/launcher.log",
           "max_concurrent": 1,
           "max_runs": 3
         }
       }
     ]
   }
   ```
   Hot-reloaded by mtime — no restart required. `project_id` may also be a
   list (`["web", "api"]`) instead of three copies of the same rule.

3. **Test**: create a task in the backlog → drag it into `Approved` → within
   a few seconds the card shows `▶ agent` and moves to `in_progress`; the agent runs on the card's model (picked in Analysis, or the project default).

It fires no matter who moves the card: a drag-drop, an MCP agent, a script
calling the REST API, or a card created directly in Approved.

## Modes

| Mode | Fired by | Model | What the agent is told | Afterwards |
|---|---|---|---|---|
| `work` (default) | `task_moved` → `approved` | the card's `agent_model` | claim the card (`kanban_pull`), do it, verify, end with `report_post(final=true)` (→ Results) | blocks the card if it was left in approved / in_progress |
| `analyse` | `task_event` `analysis_requested` (*Analyse with agent*) | the project's analyst model | read the code (read-only tools), refine story and acceptance criteria, split oversized work, pick the worker model, end with `analysis_submit` | comments *analysis submitted by …* or *finished without submitting*; the card stays in Analysis for you to approve |
| `resume` | `task_event` `message` with `agent: offline` | the session's model | `--resume <last session>`: read your messages with `inbox_get`, act, answer with `inbox_reply`; post a new result if you asked for changes | blocks only if the card was meant to be worked on and was left behind |

`resume` needs a session to continue: the card's assigned session, else the
session that proposed it. Without one the launcher comments and exits.

```json
{
  "rules": [
    {"name": "Worker on approval",
     "trigger": {"type": "task_moved", "to_status": "approved"},
     "action": {"type": "run_command", "cmd": "/abs/path/to/launch-claude.sh",
                "args": ["{task_id}", "{project_id}"], "max_concurrent": 2, "max_runs": 3}},
    {"name": "Analyst",
     "trigger": {"type": "task_event", "action": "analysis_requested"},
     "action": {"type": "run_command", "cmd": "/abs/path/to/launch-claude.sh",
                "args": ["{task_id}", "{project_id}", "analyse"], "max_concurrent": 1}},
    {"name": "Resume on message",
     "trigger": {"type": "task_event", "action": "message", "agent": "offline"},
     "action": {"type": "run_command", "cmd": "/abs/path/to/launch-claude.sh",
                "args": ["{task_id}", "{project_id}", "resume"], "max_runs": 5}}
  ]
}
```

`task_event` triggers take `action` (`analysis_requested`, `message`,
`comment`, `report`, `reply`), `agent` (`offline`: no live session and no
running command on the card; `live`: the opposite) and `from` (`human`, the
default, or `any`), so an agent's own replies never wake it up again.

## What the launcher does

1. **Preflight** — the board answers, the card is still in `approved`
   (someone may have moved it back), the project has a directory, `claude`
   is found (PATH, `CLAUDE_BIN`, or the macOS desktop app bundle) and logged
   in. A failed check moves the card to **Blocked** with the reason.
2. **Per-card lock** (`mkdir`, portable — macOS has no `flock`), on top of
   the kanban's own "one run per card" rule.
3. **Runs the agent** with
   - `--mcp-config <run dir>/mcp.json --strict-mcp-config` — the kanban
     server is handed over explicitly, so there's no dependency on the
     project's `.mcp.json` alias and nothing to approve;
   - `--permission-mode dontAsk --allowedTools …` — a tool that isn't on
     the list is denied immediately; a headless run never waits for an
     answer nobody will give;
   - its own process group, so a timeout kills claude *and* the MCP servers
     and shell commands it started.
4. **Hard timeout** (`AGENT_TIMEOUT_SEC`, default 3600): a live process is
   not proof of progress.
5. **Afterwards** — whatever is still running in the agent's process group
   is stopped. If the agent left the card in approved /
   in_progress, the launcher blocks it with the exit code, the agent's last
   words and the run folder (only if the card is still where it was — a
   card a human has moved meanwhile just gets a comment). With
   `KANBAN_GATE_CMD` set (say `pytest -q`), a card in results must also pass
   the gate.

Everything for one card lives in `~/Library/Logs/agent-kanban/runs/T-XXX/`:
`prompt.md`, `mcp.json`, `result.json` (turns, cost, session id), `agent.log`,
`launcher.log`, `gate.log`.

### Exit codes

| Code | Meaning | Kanban |
|---|---|---|
| 0 | finished; the outcome is on the card | counts as a run |
| 75 | the model's usage limit was hit | not counted against `max_runs`; card stays put |
| 1 | the launcher itself couldn't run (board unreachable) | counts as a run |

To retry cards that hit a usage limit, add a polling rule next to the
reactive one:

```json
{
  "name": "Retry approved cards nobody picked up",
  "project_id": "myproj",
  "trigger": {"type": "task_idle", "status": "approved", "minutes": 30},
  "repeat_every": {"minutes": 30},
  "action": {"type": "run_command", "cmd": "/abs/path/…/launch-claude.sh",
             "args": ["{task_id}", "{project_id}"], "max_concurrent": 1}
}
```

## Environment

Set these in the rule's `"env"` object.

| Variable | Default | Purpose |
|---|---|---|
| `KANBAN_URL` | `http://localhost:7777` | board address |
| `KANBAN_LAUNCHER_ACTOR` | `agent:launcher` | name in the card history |
| `AGENT_TIMEOUT_SEC` | `3600` | hard limit per run |
| `CLAUDE_BIN` | auto | path to the `claude` CLI |
| `CLAUDE_MODEL_S` / `_M` / `_L` | CLI default | fallback model by card size, used only when the card has no `agent_model` (opus / sonnet / haiku, chosen in Analysis) |
| `KANBAN_PERMISSION_MODE` | `dontAsk` | `acceptEdits` / `bypassPermissions` if you know why |
| `KANBAN_ALLOWED_TOOLS` | `Bash Read Edit Write Grep Glob TodoWrite` | tools besides the kanban ones (work / resume) |
| `KANBAN_ANALYST_ALLOWED_TOOLS` | `Read Grep Glob` | tools besides the kanban ones in `analyse` mode |
| `KANBAN_STRICT_MCP` | `1` | `0` also loads the project's own `.mcp.json` servers |
| `KANBAN_GATE_CMD` | — | command that must pass before a card stays in results |
| `KANBAN_RUNS_DIR` | `~/Library/Logs/agent-kanban/runs` | per-card run folders |

## Supported placeholders

Substituted into `args`:

| Placeholder | Contains |
|---|---|
| `{task_id}` | task ID (T-XXX) |
| `{title}` | task title |
| `{project_id}` | project slug |
| `{status}` | the card's column when the rule fired |
| `{from_status}` | column the card came from (empty for a created card) |
| `{to_status}` | column the card moved to |

`task_event` rules provide `{task_id}`, `{title}`, `{project_id}`, `{status}`,
`{action}`, `{actor}`, `{comment}` (the message text), `{session_id}` and
`{model}` instead of the from/to columns.

## Concurrency and runaway protection

All of this is in the kanban, not in the script:

- **One process per card and rule** — a second trigger while the first run
  is alive is ignored (the classic "two agents on one card" after a human
  drags it back and forth).
- **`max_concurrent`** — extra launches wait in a FIFO queue; `/api/automation/status`
  lists what's running and queued, and the card shows `▶ agent`.
- **`max_runs`** — after N launches for the same card it's blocked instead;
  a person moving it out of Blocked is the "try again" signal and resets the
  count (a retry rule or an agent moving it doesn't).
- **Restarts** — queued launches and running agents are stored in the
  database; a restarted server resumes the queue and keeps counting agents
  that are still working.
- **Loop guard** — a reactive rule firing on the same card more than 5 times
  in 10 minutes is skipped and reported.
- **Kill switch** — ⏸ in the top bar or `POST /api/automation/pause
  {"paused": true}`; running agents finish, nothing new starts.

## Security

The script runs on the kanban server (= localhost). There's no sandboxing —
`cmd` and `args` execute as-is. Don't put anything in `rules.json` you
wouldn't run from your own shell, and keep `KANBAN_ALLOWED_TOOLS` as narrow
as the work allows.

## Your own launcher

You can replace `launch-claude.sh` with anything else — `launch-opencode.sh`,
`launch-aider.sh`, a wrapper around another LLM. The contract:

1. Stay in the foreground while the agent works (the kanban tracks the
   process; it runs in its own session and survives kanban restarts).
2. Leave the card in a truthful column before exiting — and when that isn't
   where the agent should have put it, say why in a comment.
3. Exit 75 for "try again later" (rate limits, service down).
4. Send `X-Kanban-Actor: <your name>` with REST calls so the history shows
   who moved the card.
