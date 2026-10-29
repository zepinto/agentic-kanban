#!/usr/bin/env bash
# agentic-kanban (Mission Board) — install, connect a repo, start the board.
#
# One-liner (installs into ~/agentic-kanban, connects ~/code/myapp):
#
#   curl -fsSL https://raw.githubusercontent.com/zepinto/agentic-kanban/main/scripts/setup.sh | bash -s -- ~/code/myapp
#
# Or from a checkout:   scripts/setup.sh [REPO_DIR] [options]
#
#   REPO_DIR              code repo agents will work in (optional: without it
#                         the board is just installed and started)
#   --project SLUG        board project id (default: the repo folder's name)
#   --model M             default worker model: opus | sonnet | haiku (default sonnet)
#   --analyst-model M     model the analyst agent runs on (default sonnet)
#   --agents              add the launcher rules: worker on approval, analyst on
#                         "Analyse with agent", resume when you message a stopped agent
#   --no-hooks            don't add the message hook to REPO_DIR/.claude/settings.json
#   --channel             push messages live into sessions (Claude Code channels)
#   --dir PATH            where to install (default ~/agentic-kanban; env AGENTIC_KANBAN_DIR)
#   --branch NAME         branch to install (default main)
#   --port N              board port (default 7777)
#   --no-vendor           don't download Mermaid / Plotly.js for offline use
#   --no-start            set up, but don't start the board
#
# Safe to re-run: it updates the checkout, reuses an existing project, and
# never adds the same hook or rule twice.
set -euo pipefail

main() {
    local repo_url="${AGENTIC_KANBAN_REPO:-https://github.com/zepinto/agentic-kanban.git}"
    local dir="${AGENTIC_KANBAN_DIR:-$HOME/agentic-kanban}"
    local branch="main" port="${KANBAN_PORT:-7777}"
    local repo="" project="" model="sonnet" analyst="sonnet"
    local agents=0 hooks=1 channel=0 vendor=1 start=1

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --project) project="$2"; shift 2 ;;
            --model) model="$2"; shift 2 ;;
            --analyst-model) analyst="$2"; shift 2 ;;
            --agents) agents=1; shift ;;
            --no-hooks) hooks=0; shift ;;
            --channel) channel=1; shift ;;
            --dir) dir="$2"; shift 2 ;;
            --branch) branch="$2"; shift 2 ;;
            --port) port="$2"; shift 2 ;;
            --no-vendor) vendor=0; shift ;;
            --no-start) start=0; shift ;;
            -h|--help) if [[ -f "${BASH_SOURCE[0]:-}" ]]; then sed -n '2,26p' "${BASH_SOURCE[0]}"
                       else echo "see https://github.com/zepinto/agentic-kanban#quickstart"; fi
                       return 0 ;;
            -*) die "unknown option $1 (try --help)" ;;
            *) [[ -z "$repo" ]] || die "only one REPO_DIR, got '$repo' and '$1'"; repo="$1"; shift ;;
        esac
    done
    case "$model$analyst" in *[!a-z]*) die "models are opus, sonnet or haiku" ;; esac
    dir="${dir/#\~/$HOME}"

    # --- 1. tools -----------------------------------------------------------
    command -v git >/dev/null || die "git is required"
    if ! command -v uv >/dev/null; then
        [[ -x "$HOME/.local/bin/uv" ]] || {
            say "installing uv (https://docs.astral.sh/uv/)"
            curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
        }
        export PATH="$HOME/.local/bin:$PATH"
    fi

    # --- 2. checkout --------------------------------------------------------
    if [[ -d "$dir/.git" ]]; then
        say "updating $dir ($branch)"
        git -C "$dir" fetch --quiet origin "$branch"
        git -C "$dir" checkout --quiet "$branch"
        git -C "$dir" merge --quiet --ff-only "origin/$branch" \
            || warn "local changes in $dir; kept them and skipped the update"
    else
        say "cloning into $dir"
        git clone --quiet --branch "$branch" "$repo_url" "$dir"
    fi
    say "installing Python dependencies"
    (cd "$dir" && uv sync --quiet)
    local py="$dir/.venv/bin/python"
    if (( vendor )); then
        bash "$dir/scripts/vendor_libs.sh" >/dev/null 2>&1 \
            && say "Mermaid and Plotly.js stored locally" \
            || warn "couldn't download Mermaid/Plotly.js; the board will use the CDN"
    fi

    # --- 3. project, connect, hooks, rules -----------------------------------
    if [[ -n "$repo" ]]; then
        local given="$repo"
        repo="$(cd "${repo/#\~/$HOME}" 2>/dev/null && pwd)" || die "no such directory: $given"
        [[ -n "$project" ]] || project="$(basename "$repo")"
        project="$(printf '%s' "$project" | tr '[:upper:]_ .' '[:lower:]---' | tr -cd 'a-z0-9-' \
                   | sed 's/^-*//; s/-*$//' | cut -c1-32)"
        [[ ${#project} -ge 2 ]] || die "can't make a project id from '$repo'; pass --project"

        (cd "$dir" && "$py" - "$project" "$repo" "$model" "$analyst" <<'PY')
import sys
from pathlib import Path
from kanban_store import Store
slug, repo, model, analyst = sys.argv[1:]
s = Store()
p = s.get_project(slug)
if p is None:
    s.create_project(slug, Path(repo).name, path=repo, default_model=model, analyst_model=analyst)
    print(f"  project '{slug}' created ({model} workers, {analyst} analyst)")
else:
    s.update_project(slug, path=repo)
    print(f"  project '{slug}' exists; directory set to {repo}")
PY
        local ch=(--no-channel); (( channel )) && ch=(--channel)
        local out
        out="$(cd "$dir" && "$py" -m kanban_mcp.connect "$repo" --project "$project" "${ch[@]}" 2>&1)" \
            || { echo "$out" >&2; die "connecting $repo failed"; }
        say "connected $repo: .mcp.json and CLAUDE.md written"

        if (( hooks )); then
            # PYTHONPATH + KANBAN_DB: the hook runs from REPO_DIR, not from the board's folder
            "$py" - "$repo/.claude/settings.json" \
                "KANBAN_DB='$dir/tasks.db' PYTHONPATH='$dir' '$py' -m kanban_mcp.hook" <<'PY'
import json, sys
from pathlib import Path
f, cmd = Path(sys.argv[1]), sys.argv[2]
data = json.loads(f.read_text()) if f.exists() and f.read_text().strip() else {}
hooks = data.setdefault("hooks", {})
added = 0
for event in ("Stop", "UserPromptSubmit"):
    groups = hooks.setdefault(event, [])
    if any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
        continue
    # replace an older kanban hook (another install path) instead of doubling it
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if "kanban_mcp.hook" not in str(h.get("command"))]
    groups[:] = [g for g in groups if g.get("hooks")]
    groups.append({"hooks": [{"type": "command", "command": cmd}]})
    added += 1
f.parent.mkdir(parents=True, exist_ok=True)
f.write_text(json.dumps(data, indent=2) + "\n")
print(f"  message hook {'added to' if added else 'already in'} {f}")
PY
        fi
    fi

    if (( agents )); then
        "$py" - "$dir/kanban_data/rules.json" "$dir/examples/agent-launcher/launch-claude.sh" <<'PY'
import json, sys
from pathlib import Path
f, cmd = Path(sys.argv[1]), sys.argv[2]
data = json.loads(f.read_text()) if f.exists() and f.read_text().strip() else {}
rules = data.setdefault("rules", [])
have = {r.get("name") for r in rules}
new = [
    {"name": "Mission Board: worker on approval",
     "trigger": {"type": "task_moved", "to_status": "approved"},
     "action": {"type": "run_command", "cmd": cmd, "args": ["{task_id}", "{project_id}"],
                "max_concurrent": 2, "max_runs": 3}},
    {"name": "Mission Board: analyst",
     "trigger": {"type": "task_event", "action": "analysis_requested"},
     "action": {"type": "run_command", "cmd": cmd, "args": ["{task_id}", "{project_id}", "analyse"],
                "max_concurrent": 1}},
    {"name": "Mission Board: resume on message",
     "trigger": {"type": "task_event", "action": "message", "agent": "offline"},
     "action": {"type": "run_command", "cmd": cmd, "args": ["{task_id}", "{project_id}", "resume"],
                "max_runs": 5}},
]
added = [r for r in new if r["name"] not in have]
rules.extend(added)
f.parent.mkdir(parents=True, exist_ok=True)
f.write_text(json.dumps(data, indent=2) + "\n")
print(f"  agent rules: {len(added)} added, {len(new) - len(added)} already there ({f})")
PY
        command -v claude >/dev/null \
            || warn "'claude' is not on PATH: install Claude Code and log in, or set CLAUDE_BIN in the rules' env"
    fi

    # --- 4. start -------------------------------------------------------------
    echo
    say "ready — board at http://localhost:$port${project:+/p/$project}"
    if [[ -n "$repo" ]]; then
        local flags=""
        (( channel )) && flags=" --dangerously-load-development-channels server:agent-kanban"
        echo "   work with agents: cd $repo && claude$flags"
    fi
    (( agents )) || echo "   to have the board start agents on approval, re-run with --agents"
    if (( start )); then
        echo "   starting the board (Ctrl-C to stop; start again with: cd $dir && uv run python -m kanban_ui)"
        cd "$dir"
        KANBAN_PORT="$port" exec uv run --quiet python -m kanban_ui </dev/null
    else
        echo "   start it with: cd $dir && KANBAN_PORT=$port uv run python -m kanban_ui"
    fi
}

say()  { printf '\033[1;34m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

main "$@"
