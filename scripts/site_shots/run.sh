#!/usr/bin/env bash
# Retake the website's dashboard screenshots (site/img/{now,round,agents,goal}-{light,dark}*.webp)
# from a made-up demo: its own HOME, database and project folders in a scratch directory,
# Linear off, no claude or gh. Needs the dev venv (websockets) and a Chromium headless shell.
#   scripts/site_shots/run.sh [out_dir]   (default site/img; APO_SHOTS_PORT picks the port)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
out="${1:-$repo/site/img}"
port="${APO_SHOTS_PORT:-8475}"
server=""
scratch="$(mktemp -d "${TMPDIR:-/tmp}/apo-site-shots.XXXXXX")"
trap 'kill "$server" 2>/dev/null || true; rm -rf "$scratch"' EXIT
if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then echo "port $port is in use" >&2; exit 1; fi
mkdir -p "$scratch/home/projects"
export APO_CHROME="${APO_CHROME:-$HOME/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell}"
py="$repo/.venv/bin/python"
export HOME="$scratch/home" APO_DB="$scratch/demo.db" APO_OFFLINE=1 APO_LINEAR_API_KEY= APO_LINEAR_TEAM= \
  APO_CLAUDE_BIN="$scratch/no-claude" APO_GH_BIN="$scratch/no-gh" CLAUDE_CONFIG_DIR="$scratch/home/.claude" \
  APO_PROJECTS_ROOT="$scratch/home/projects" APO_POLL_SECONDS=100000 PYTHONPATH="$repo/src"
unset XDG_DATA_HOME XDG_CONFIG_HOME XDG_STATE_HOME XDG_CACHE_HOME CLAUDE_CODE_SESSION_ID APO_CLAUDE_CONFIG_DIRS
"$py" "$here/seed.py" "$scratch"
cd "$repo"
"$py" -m uvicorn apo.web.app:app --host 127.0.0.1 --port "$port" > "$scratch/server.log" 2>&1 &
server=$!
for _ in $(seq 50); do (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && break; sleep 0.2; done
"$py" "$here/shoot.py" "http://127.0.0.1:$port" "$out"
