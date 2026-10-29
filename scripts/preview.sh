#!/usr/bin/env bash
# Run this checkout as a second dashboard on a copy of the live data, to look at a
# branch before it's merged:   scripts/preview.sh [port]   (default 8432)
#
# Safe to click around in. The copy's repositories, and the folders of the sessions
# it lists, point at empty scratch folders, so answering writes nothing into your real repos, and New project offers scratch
# copies of your projects folder's subfolders, never the real ones. Linear is off;
# Claude Code can only be listed (claude agents) or asked for a recap (claude -p,
# which reads the transcript and writes only to the copy), never started or
# stopped. Every page shows a Preview banner. Run it again to refresh the copy.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
port="${1:-8432}"
work="${TMPDIR:-/tmp}/apo-preview-$port"
python_bin="${PYTHON:-}"
if [ -z "$python_bin" ]; then
  for candidate in "$here/.venv/bin/python" "$(dirname "$(git -C "$here" rev-parse --path-format=absolute --git-common-dir)")/.venv/bin/python"; do
    if [ -x "$candidate" ]; then python_bin="$candidate"; break; fi
  done
fi
[ -n "$python_bin" ] || { echo "No virtualenv found; set PYTHON=/path/to/python" >&2; exit 1; }
live_db="${APO_LIVE_DB:-$("$python_bin" -c 'from apo import config; print(config.get_db_path())')}"
projects_root="$("$python_bin" -c 'from apo import config; print(config.get_projects_root())')"

if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
  echo "Port $port is already in use — pass another: scripts/preview.sh 8433" >&2
  exit 1
fi

rm -rf "$work/repos" "$work/projects" "$work/elsewhere"
mkdir -p "$work/repos" "$work/projects"

# Sessions' folders move onto the scratch folders too. The dashboard registers a
# session's folder as a repository when none has it, and with a real folder there the
# preview could write .claude/decisions/ into it.
cat > "$work/remap.py" <<'PY'
import json
import os
import sys


def remap(path, folders, work):
    if not path:
        return path
    real = os.path.realpath(path)
    home = os.path.realpath(os.path.expanduser("~"))
    if real == home or home.startswith(real.rstrip("/") + "/"):
        return path  # never registered as a repository
    for original, folder in folders:
        if real == original or real.startswith(original + "/"):
            return folder + real[len(original):]
    return os.path.join(work, "elsewhere", real.lstrip("/"))


if __name__ == "__main__":
    work = sys.argv[1]
    with open(os.path.join(work, "folders.json")) as f:
        folders = json.load(f)
    agents = json.load(sys.stdin)
    for agent in agents:
        if isinstance(agent, dict) and agent.get("cwd"):
            agent["cwd"] = remap(agent["cwd"], folders, work)
    json.dump(agents, sys.stdout)
PY

"$python_bin" - "$live_db" "$work" "$projects_root" <<'PY'
import json
import os
import sqlite3
import sys

live, work, root = sys.argv[1], sys.argv[2], os.path.realpath(os.path.expanduser(sys.argv[3]))
sys.path.insert(0, work)
from remap import remap
mirror = os.path.join(work, "projects")
if os.path.isdir(root):
    for name in os.listdir(root):
        real = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(real):
            continue
        os.makedirs(os.path.join(mirror, name, ".git") if os.path.exists(os.path.join(real, ".git"))
                    else os.path.join(mirror, name), exist_ok=True)
dest = os.path.join(work, "orchestrator.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(dest + suffix)
    except FileNotFoundError:
        pass
src = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
out = sqlite3.connect(dest)
src.backup(out)
src.close()
folders = []
for repo_id, original in out.execute("SELECT id, local_path FROM repositories").fetchall():
    name = os.path.basename(original.rstrip("/")) or "repo"
    # A folder directly in the projects root maps onto its mirror, so New project shows it as taken.
    in_root = os.path.dirname(os.path.realpath(original.rstrip("/"))) == root
    folder = os.path.join(mirror, name) if in_root else os.path.join(work, "repos", str(repo_id), name)
    os.makedirs(folder, exist_ok=True)
    out.execute("UPDATE repositories SET local_path = ? WHERE id = ?", (folder, repo_id))
    folders.append((os.path.realpath(original.rstrip("/")), folder))
folders.sort(key=lambda pair: len(pair[0]), reverse=True)
with open(os.path.join(work, "folders.json"), "w") as f:
    json.dump(folders, f)
for session_id, cwd in out.execute("SELECT id, cwd FROM sessions WHERE cwd IS NOT NULL").fetchall():
    out.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (remap(cwd, folders, work), session_id))
out.commit()
out.close()
print(f"Preview data: a copy of {live} in {work}")
PY

cat > "$work/claude" <<SH
#!/bin/sh
case "\$1" in
  agents) claude "\$@" | "$python_bin" "$work/remap.py" "$work" ;;
  -p) exec claude "\$@" ;;
  *) echo "Preview: starting or stopping Claude Code sessions is turned off here" >&2; exit 1 ;;
esac
SH
chmod +x "$work/claude"

echo "Preview of $(git -C "$here" branch --show-current) at http://127.0.0.1:$port"
cd "$here"
APO_PREVIEW=1 APO_OFFLINE=1 APO_DB="$work/orchestrator.db" APO_CLAUDE_BIN="$work/claude" APO_POLL_SECONDS=100000 \
  APO_PROJECTS_ROOT="$work/projects" APO_DASHBOARD_AUTOSTART= \
  APO_LINEAR_API_KEY= APO_LINEAR_TEAM= exec "$python_bin" -m uvicorn apo.web.app:app --host 127.0.0.1 --port "$port" --reload
