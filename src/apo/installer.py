"""`apo init`: sets up this machine to run apo with Claude Code.

It creates the data folder, saves the answers it asks for in the settings file
(config.config_file()), and, in the Claude Code profile it runs under
(CLAUDE_CONFIG_DIR, else ~/.claude): registers the MCP server `apo` at user scope,
adds the hooks that call the installed `apo`, and copies in the `/decisions` skill.

Run again, it replaces its own entries rather than adding more, and that includes the
ones an older install left under the old names (the `ai-decision-orchestrator` MCP
server, `ado session-*` hooks, the hook script and skill linked from a checkout). It
never touches a hook, a skill or a setting it didn't write.
"""
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from importlib.resources import files
from pathlib import Path

import apo
from apo import claude_runtime, config

MCP_NAME = "apo"
LEGACY_MCP_NAME = "ai-decision-orchestrator"
NUDGE_SCRIPT = "session-decisions-nudge.sh"
SKILL_MARK = "<!-- Installed by apo"

# Seconds: longer than db.PROMPT_HOLD_SECONDS (an hour).
PROMPT_HOOK_TIMEOUT = 3660

# (event, `apo` subcommand, hook options). Observe-only hooks are async so they never
# sit in the agent's turn. Stop stays synchronous with a short timeout: it writes the
# turn's final state, and a hung Stop hook would BLOCK the agent's turn.
SESSION_HOOKS = (
    ("SessionStart", "session-start", {}),
    ("PostToolUse", "session-heartbeat", {"run_async": True}),
    ("Stop", "session-stop", {"timeout": 2}),
    ("SessionEnd", "session-end", {}),
    ("TaskCreated", "checklist-item-created", {"run_async": True}),
    ("TaskCompleted", "checklist-item-completed", {"run_async": True}),
    # Synchronous: its output hands the asking session the answers to what it filed
    # (async hook output is ignored). Short timeout, like Stop.
    ("UserPromptSubmit", "session-prompt", {"timeout": 2}),
    # The states that need the human. Notification only observes. PermissionRequest and
    # PreToolUse are synchronous because a background session's prompt is held for the
    # dashboard and its answer returned (db.PROMPT_HOLD_SECONDS); the timeout outlasts
    # that hold so the hook always cleans up. A terminal session's hook returns at once,
    # leaving Claude Code's own dialog in charge.
    ("Notification", "session-notification", {"run_async": True}),
    ("PermissionRequest", "session-permission", {"timeout": PROMPT_HOOK_TIMEOUT}),
    ("PreToolUse", "session-pretool", {"timeout": PROMPT_HOOK_TIMEOUT, "matcher": "AskUserQuestion|ExitPlanMode"}),
    ("StopFailure", "session-stop-failure", {"run_async": True}),
)
_OUR_SUBCOMMANDS = {subcommand for _, subcommand, _ in SESSION_HOOKS}


def packaged(name: str):
    """A file shipped in the package's resources/ folder."""
    return files("apo") / "resources" / name


def apo_executable() -> str:
    """The installed `apo`, as an absolute path when it can be found: Claude Code runs
    hooks and MCP servers from its own process, whose PATH may not be this shell's."""
    found = shutil.which("apo")
    if found:
        return found
    beside = Path(sys.executable).parent / "apo"
    return str(beside) if beside.exists() else "apo"


def write_env_updates(env_path: Path, updates: dict[str, str]) -> None:
    """Merge `updates` into env_path's KEY=VALUE lines, preserving every other line
    and overwriting a key already present rather than duplicating it. The file can
    hold a Linear key, so only its owner can read it."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    seen = set()
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                result.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        result.append(line)
    for key, value in updates.items():
        if key not in seen:
            result.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    _write_private(env_path, "\n".join(result) + "\n")


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)
    os.chmod(path, 0o600)


def _write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.apo-tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _backup(path: Path) -> Path:
    backup_path = path.with_name(f"{path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    backup_path.write_text(path.read_text())
    return backup_path


def is_our_hook(command: str) -> bool:
    """A hook this installer, or an older one under the old name, wrote: the nudge
    script, or `apo`/`ado` with one of the session subcommands, at any path."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) == 1:
        return Path(parts[0]).name == NUDGE_SCRIPT
    return len(parts) == 2 and Path(parts[0]).name in ("apo", "ado") and parts[1] in _OUR_SUBCOMMANDS


def _hook_entry(command: str, timeout: int | None = None, run_async: bool = False,
                matcher: str | None = None) -> dict:
    hook = {"type": "command", "command": command}
    if timeout is not None:
        hook["timeout"] = timeout
    if run_async:
        hook["async"] = True
    entry = {"hooks": [hook]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def merge_hooks(settings: dict, apo_bin: str, nudge_path: Path) -> dict:
    """`settings` with every hook of ours (by is_our_hook, any path, either name)
    removed and the current set added once. Everyone else's hooks stay as they were."""
    hooks = settings.setdefault("hooks", {})
    for event in list(hooks):
        kept = []
        for entry in hooks[event]:
            entry_hooks = [h for h in entry.get("hooks", []) if not is_our_hook(h.get("command", ""))]
            if entry_hooks:
                kept.append({**entry, "hooks": entry_hooks})
            elif not entry.get("hooks"):
                kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    hooks.setdefault("SessionStart", []).append(_hook_entry(shlex.quote(str(nudge_path))))
    quoted = shlex.quote(apo_bin)
    for event, subcommand, options in SESSION_HOOKS:
        hooks.setdefault(event, []).append(_hook_entry(f"{quoted} {subcommand}", **options))
    return settings


def rename_permissions(settings: dict) -> dict:
    """Allow-list entries for the old server's tools, moved to the new name."""
    permissions = settings.get("permissions")
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    if not isinstance(allow, list):
        return settings
    old = f"mcp__{LEGACY_MCP_NAME}"
    renamed = []
    for rule in allow:
        if isinstance(rule, str) and (rule == old or rule.startswith(old + "__")):
            rule = f"mcp__{MCP_NAME}" + rule[len(old):]
        if rule not in renamed:
            renamed.append(rule)
    permissions["allow"] = renamed
    return settings


def install_hooks(settings_path: Path, apo_bin: str, nudge_path: Path) -> str:
    """Rewrites settings.json with our hooks and renamed permissions: backed up once
    before the first change, written atomically, and left alone if it isn't JSON."""
    before_text = settings_path.read_text() if settings_path.exists() else None
    try:
        settings = json.loads(before_text) if before_text and before_text.strip() else {}
    except json.JSONDecodeError as exc:
        return f"skipped: {settings_path} isn't valid JSON ({exc}); fix it and run `apo init` again"
    if not isinstance(settings, dict):
        return f"skipped: {settings_path} doesn't hold a JSON object"
    before = json.loads(json.dumps(settings))
    after = rename_permissions(merge_hooks(settings, apo_bin, nudge_path))
    if after == before:
        return f"already up to date in {settings_path}"
    backup = _backup(settings_path) if before_text is not None else None
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(settings_path, json.dumps(after, indent=2) + "\n")
    return f"written to {settings_path}" + (f" (the old one is saved as {backup.name})" if backup else "")


def install_nudge_script(dest: Path) -> Path:
    """The session-start notice is plain shell, so it runs without Python. It's copied
    out of the package because a path into an install moves when the install does."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(packaged(f"hooks/{NUDGE_SCRIPT}").read_text())
    dest.chmod(0o755)
    return dest


def _skill_text() -> str:
    text = packaged("skills/decisions/SKILL.md").read_text()
    _, front, body = text.split("---", 2)
    mark = f"{SKILL_MARK} {apo.__version__}. `apo init` keeps this file up to date, so edits here are overwritten. -->"
    return f"---{front}---\n\n{mark}\n{body}"


def _is_old_skill_link(path: Path) -> bool:
    """The symlink an older install made into its checkout's skills/decisions."""
    try:
        return ".claude/decisions/INDEX.md" in (path / "SKILL.md").read_text()
    except OSError:
        return not path.exists()


def install_skill(dest_dir: Path) -> str:
    """A copy of the packaged skill, marked as apo's so a later run knows it may
    replace it. Someone else's `decisions` skill is left alone."""
    skill = dest_dir / "SKILL.md"
    if dest_dir.is_symlink():
        if not _is_old_skill_link(dest_dir):
            return f"skipped: {dest_dir} links to a skill apo didn't install"
        dest_dir.unlink()
    elif dest_dir.exists() and not (skill.is_file() and SKILL_MARK in skill.read_text()):
        return f"skipped: {dest_dir} holds a skill apo didn't install"
    status = "updated" if skill.exists() else "installed"
    dest_dir.mkdir(parents=True, exist_ok=True)
    skill.write_text(_skill_text())
    return f"{status} in {dest_dir}"


def _manual_mcp_command(apo_bin: str) -> str:
    return f"claude mcp add {MCP_NAME} --scope user -- {shlex.quote(apo_bin)} mcp"


def register_mcp_server(apo_bin: str) -> tuple[bool, str]:
    """`claude mcp add apo --scope user -- <apo> mcp`, after removing any earlier
    registration under either name, so a re-run or a moved install updates the entry
    instead of failing on it. Never raises: a missing `claude` or a failed add comes
    back as a message."""
    claude = shutil.which(config.claude_bin())
    if claude is None:
        return False, f"`claude` isn't on PATH. Register the MCP server yourself:\n  {_manual_mcp_command(apo_bin)}"
    replaced = []
    for name in (LEGACY_MCP_NAME, MCP_NAME):
        removed = subprocess.run([claude, "mcp", "remove", name, "--scope", "user"], capture_output=True, text=True)
        if removed.returncode == 0 and name == LEGACY_MCP_NAME:
            replaced.append(name)
    result = subprocess.run([claude, "mcp", "add", MCP_NAME, "--scope", "user", "--", apo_bin, "mcp"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or "claude mcp add failed"
        return False, f"{detail}\nRegister it yourself:\n  {_manual_mcp_command(apo_bin)}"
    note = f" (replacing the old {replaced[0]} entry)" if replaced else ""
    return True, f"registered as `{MCP_NAME}` at user scope{note}"


def _ask(prompt: str, args) -> str:
    return "" if args.yes else input(prompt).strip()


def _settings_answers(args) -> dict[str, str]:
    updates = {}
    db_path = args.db_path or _ask(f"Where should apo keep its database? [{config.get_db_path()}]: ", args)
    if db_path:
        updates["APO_DB"] = str(Path(db_path).expanduser())

    projects_root = args.projects_root or _ask(f"Where do your projects live? [{config.get_projects_root()}]: ", args)
    if projects_root:
        root = Path(projects_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        updates["APO_PROJECTS_ROOT"] = str(root)

    if not args.skip_linear:
        api_key, team = args.linear_api_key, args.linear_team
        if not api_key and _ask("Connect Linear now? You can also do it later in the dashboard's Settings. [y/N]: ",
                                args).lower() == "y":
            api_key = getpass.getpass("Linear API key: ").strip()
            team = input("Linear team (key or id): ").strip()
        if api_key and team:
            updates.update({"APO_LINEAR_API_KEY": api_key, "APO_LINEAR_TEAM": team})
    return updates


def run(args) -> None:
    updates = _settings_answers(args)
    if updates:
        write_env_updates(config.config_file(), updates)
        print(f"Settings: saved to {config.config_file()}")

    for folder in (config.data_dir(), config.get_db_path().parent):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = config.get_db_path().parent
    print(f"Data: {data}")

    profile = claude_runtime.claude_config_dir()
    apo_bin = apo_executable()

    if args.skip_mcp:
        print("MCP server: skipped (--skip-mcp)")
    else:
        print(f"MCP server: {register_mcp_server(apo_bin)[1]}")

    if args.skip_hook:
        print("Hooks: skipped (--skip-hook)")
    else:
        nudge = install_nudge_script(config.data_dir() / "hooks" / NUDGE_SCRIPT)
        print(f"Hooks: {install_hooks(profile / 'settings.json', apo_bin, nudge)}")

    if args.skip_skill:
        print("Skill /decisions: skipped (--skip-skill)")
    else:
        print(f"Skill /decisions: {install_skill(profile / 'skills' / 'decisions')}")

    print(f"""
apo init complete.

Next:
  - Start a new Claude Code session. Sessions already running keep the tools they started with.
  - Start the dashboard with `apo dashboard start`, then open http://127.0.0.1:{config.dashboard_port()}
  - Your data lives in {data}""")
