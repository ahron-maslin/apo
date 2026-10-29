"""Read-only, zero-token view of the Claude Code processes running on this machine.

Two sources, both local and free (no LLM is ever woken):
- ~/.claude/sessions/<pid>.json: the record Claude Code keeps per running process,
  rewritten when that process switches session (/clear, /resume). Undocumented, so
  every field is treated as optional and the record is only trusted when it provably
  belongs to the live process with that pid (pids get reused).
- `claude agents --json`: the documented scripting interface over the same data, used
  as the fallback. It costs a process spawn (~0.5s), so it's never on a hook's path.
"""
import json
import os
import re
import subprocess
from pathlib import Path


class LaunchError(RuntimeError):
    pass


def _binary() -> str:
    return os.environ.get("ADO_CLAUDE_BIN") or "claude"


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def claude_config_dirs() -> list[Path]:
    """Every Claude Code profile whose sessions this machine runs: this process's own
    first, then any others named in ADO_CLAUDE_CONFIG_DIRS (os.pathsep-separated). A
    session started under another CLAUDE_CONFIG_DIR (a work account, say) reports in
    through its hooks like any other, but its process record and `claude agents`
    listing live in that profile — without it here, reconcile ends those sessions as
    gone while they are still running."""
    dirs = [claude_config_dir()]
    for raw in (os.environ.get("ADO_CLAUDE_CONFIG_DIRS") or "").split(os.pathsep):
        path = Path(raw.strip()).expanduser() if raw.strip() else None
        if path and path not in dirs:
            dirs.append(path)
    return dirs


def process_start_ticks(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat (start time in clock ticks), which Claude Code
    stores as procStart. Parsed after the last ')' since the command name can itself
    contain spaces and parentheses."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = data[data.rindex(")") + 2:].split()
    return fields[19] if len(fields) > 19 else None


def session_for_pid(pid: int) -> dict | None:
    for folder in claude_config_dirs():
        try:
            record = json.loads((folder / "sessions" / f"{pid}.json").read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or not record.get("sessionId"):
            continue
        start = process_start_ticks(pid)
        if start is not None and str(record.get("procStart")) == start:
            return record
    return None


def list_agents(timeout: float = 5.0) -> list[dict] | None:
    """None when the CLI is unavailable or its output is unusable — callers treat
    that as "no information", never as "no sessions". Covers every profile in
    claude_config_dirs(), and is None if any one of them can't be listed: half a
    picture would end every live session of the profile that failed."""
    merged: list[dict] = []
    for index, folder in enumerate(claude_config_dirs()):
        # This process's own profile runs with the environment untouched: setting
        # CLAUDE_CONFIG_DIR to the default ~/.claude also moves ~/.claude.json.
        env = None if index == 0 else {**os.environ, "CLAUDE_CONFIG_DIR": str(folder)}
        agents = _list_agents_in(env, timeout)
        if agents is None:
            return None
        merged.extend(agents)
    return merged


def _list_agents_in(env: dict | None, timeout: float) -> list[dict] | None:
    try:
        result = subprocess.run(
            [_binary(), "agents", "--json"], capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = data.get("agents")
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else None


def current_session_id(pid: int | None = None) -> str | None:
    """The live session id of Claude Code process `pid` — by default this process's
    parent, which is what a stdio MCP server is spawned by."""
    pid = pid or os.getppid()
    record = session_for_pid(pid)
    if record:
        return record["sessionId"]
    for agent in list_agents() or []:
        if agent.get("pid") == pid and agent.get("sessionId"):
            return agent["sessionId"]
    return None


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def _run(args: list[str], cwd: str | None, timeout: float, input: str | None = None) -> str:
    try:
        result = subprocess.run([_binary(), *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                                **({"input": input} if input is not None else {"stdin": subprocess.DEVNULL}))
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchError(f"Couldn't run claude: {exc}") from exc
    if result.returncode != 0:
        raise LaunchError((result.stderr or result.stdout).strip() or f"claude exited with {result.returncode}")
    return result.stdout


# What a project's autonomy may ask for. Modes that skip permission checks altogether
# are deliberately not here.
PERMISSION_MODES = ("acceptEdits", "plan")


def start_background(cwd: str, prompt: str, resume: str | None = None, timeout: float = 30.0,
                     permission_mode: str | None = None) -> str | None:
    """`claude --bg` returns as soon as the session is up and prints the short id
    that `claude agents`, `stop` and `attach` take. Permission prompts in that
    session still surface in Claude Code's own agent view — nothing here approves
    anything on the agent's behalf."""
    if prompt.lstrip().startswith("-"):
        raise LaunchError("A prompt can't start with '-' (claude would read it as an option).")
    if resume is not None and not _SAFE_ID.fullmatch(resume):
        raise LaunchError(f"Not a session id: {resume!r}")
    if permission_mode is not None and permission_mode not in PERMISSION_MODES:
        raise LaunchError(f"Not an allowed permission mode: {permission_mode!r}")
    args = ["--bg", *(["--permission-mode", permission_mode] if permission_mode else []),
            *(["--resume", resume] if resume else []), prompt]
    output = _run(args, cwd, timeout)
    match = re.search(r"\b[0-9a-f]{8}\b", output)
    return match.group(0) if match else None


def stop_background(agent_id: str, timeout: float = 15.0) -> bool:
    if not _SAFE_ID.fullmatch(agent_id):
        raise LaunchError(f"Not a background session id: {agent_id!r}")
    _run(["stop", agent_id], None, timeout)
    return True


RECAP_PROMPT = (
    "The text on stdin is a Claude Code session: what the user asked, what Claude said, and the tools it ran. "
    "Write a recap for the user coming back to it: what it was asked to do, what it did, where it stands now, "
    "and what's left or waiting on them. Three to six sentences of plain prose, no headings or lists."
)


def summarize(text: str, timeout: float = 120.0) -> str:
    """A recap from a small model through Claude Code's own sign-in, so no API key.
    Hooks, settings files, tools, MCP servers and the saved session are all off: this
    call must not show up on the dashboard as a session of its own."""
    args = ["-p", "--model", "haiku", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--settings", json.dumps({"disableAllHooks": True}), "--no-session-persistence", RECAP_PROMPT]
    return _run(args, None, timeout, input=text).strip()

