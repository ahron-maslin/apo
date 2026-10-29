"""Sets up a teammate's own machine to run their own instance of apo: DB location,
optional Linear config, MCP server registration, and installing the session-start
hook + `decisions` skill into their Claude Code config.

Each teammate runs this against their own home directory and their own DB — there is
no shared server, so nothing here needs auth. It writes into ~/.claude, not into
whatever checkout ships this code, so config survives independently of the checkout
(see config.home_env_path()).
"""
import getpass
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


def write_env_updates(env_path: Path, updates: dict[str, str]) -> None:
    """Merge `updates` into env_path's KEY=VALUE lines, preserving every other line
    and overwriting a key already present rather than duplicating it."""
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
    env_path.write_text("\n".join(result) + "\n")


def install_skill_symlink(source: Path, dest: Path) -> str:
    """Idempotent: a no-op if dest already correctly points at source, and never
    clobbers something else already there (a foreign skill, a real directory)."""
    if dest.is_symlink():
        return "already-linked" if dest.resolve() == source.resolve() else "skipped-exists"
    if dest.exists():
        return "skipped-exists"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(source, target_is_directory=source.is_dir())
    return "created"


def _backup(path: Path) -> None:
    backup_path = path.with_name(f"{path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    backup_path.write_text(path.read_text())


def merge_hook(
    settings_path: Path, event: str, command: str, timeout: int | None = None, backup: bool = True,
    run_async: bool = False, matcher: str | None = None,
) -> None:
    """Read-merge-write one event's hooks array in settings.json: adds `command`
    without disturbing any other hook already registered there (for this event or any
    other), without duplicating it on re-run, and takes a timestamped backup before
    touching an existing file. `timeout` (seconds) matters for hooks that can block
    the agent's turn (Stop) — a hung process shouldn't hang the user's session.
    `run_async` is for observe-only hooks: Claude Code runs them in the background,
    so they never add latency to the agent's turn (their output is ignored).

    Re-running with the same command updates that entry's timeout/async in place, so
    an older install picks up a changed hook spec on the next `apo init`.

    `backup=False` is for a caller (run(), below) that's about to make several of
    these calls in one pass and already took a single backup representing the
    state before any of them ran — taking one here per call would otherwise leave
    a pile of redundant copies of what is, modulo the merge itself, the same file."""
    if settings_path.exists():
        if backup:
            _backup(settings_path)
        data = json.loads(settings_path.read_text())
    else:
        data = {}

    bucket = data.setdefault("hooks", {}).setdefault(event, [])
    entry, hook = next(
        ((e, h) for e in bucket for h in e.get("hooks", []) if h.get("command") == command), (None, None),
    )
    if hook is None:
        hook = {"type": "command", "command": command}
        entry = {"hooks": [hook]}
        bucket.append(entry)
    for key, value in (("timeout", timeout), ("async", True if run_async else None)):
        if value is None:
            hook.pop(key, None)
        else:
            hook[key] = value
    # A matcher applies to every hook in its entry, so only manage it on an entry
    # that holds just this one.
    if len(entry.get("hooks", [])) == 1:
        if matcher is None:
            entry.pop("matcher", None)
        else:
            entry["matcher"] = matcher

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")


def register_mcp_server(command: list[str] | None = None) -> tuple[bool, str]:
    """Registers the MCP server via the `claude` CLI, pointed at the installed `apo`
    command (`apo mcp`) rather than a raw path into a checkout. Never raises —
    a missing `claude` binary or a failed registration (e.g. already registered)
    degrades to a message instead of crashing the wizard.

    Resolves `apo` to an absolute path when possible, rather than registering the
    bare name: Claude Code will later exec that command from its own process, whose
    PATH may not match the shell `apo init` ran in (e.g. a project-only venv that
    isn't on the system PATH)."""
    if shutil.which("claude") is None:
        return False, (
            "`claude` CLI not found on PATH — register the MCP server manually:\n"
            "  claude mcp add apo --scope user -- apo mcp"
        )
    if command is None:
        apo_bin = shutil.which("apo") or "apo"
        command = ["claude", "mcp", "add", "apo", "--scope", "user", "--", apo_bin, "mcp"]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr.strip() or "claude mcp add failed"
    return True, "Registered the MCP server with Claude Code."


# Seconds: longer than db.PROMPT_HOLD_SECONDS (an hour).
PROMPT_HOOK_TIMEOUT = 3660

# (event, `apo` subcommand, merge_hook options). Observe-only hooks are async so they
# never sit in the agent's turn. Stop stays synchronous with a short timeout: it
# writes the turn's final state, and a hung Stop hook would BLOCK the agent's turn.
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


def run(args, home_dir: Path | None = None) -> None:
    from apo import config

    checkout_root = Path(__file__).resolve().parent / "resources"
    home_dir = home_dir or Path.home()
    home_env_path = home_dir / ".claude" / "ai-decision-orchestrator" / ".env"

    db_path = args.db_path
    if db_path is None and not args.yes:
        entered = input(f"DB location [{config.default_db_path()}]: ").strip()
        db_path = entered or None
    if db_path:
        write_env_updates(home_env_path, {"APO_DB": str(Path(db_path).expanduser())})
        print(f"DB location: {db_path}")
    else:
        print(f"DB location: {config.default_db_path()} (default)")

    # The folder the dashboard's New project offers subfolders of (config.get_projects_root).
    projects_root = args.projects_root
    if projects_root is None and not args.yes:
        entered = input(f"Where do your projects live? [{config.get_projects_root()}]: ").strip()
        projects_root = entered or None
    if projects_root:
        root = Path(projects_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        write_env_updates(home_env_path, {"APO_PROJECTS_ROOT": str(root)})
        print(f"Projects folder: {root}")
    else:
        print(f"Projects folder: {config.get_projects_root()}")

    if args.skip_linear:
        print("Linear skipped (--skip-linear).")
    else:
        api_key, team = args.linear_api_key, args.linear_team
        if not args.yes and not api_key:
            if input("Configure Linear? [y/N]: ").strip().lower() == "y":
                api_key = getpass.getpass("Linear API key: ").strip()
                team = input("Linear team (key or id): ").strip()
        if api_key and team:
            write_env_updates(home_env_path, {"APO_LINEAR_API_KEY": api_key, "APO_LINEAR_TEAM": team})
            print("Linear configured.")
        else:
            print("Linear skipped.")

    if args.skip_mcp:
        print("MCP server registration skipped (--skip-mcp).")
    else:
        _, message = register_mcp_server()
        print(message)

    if args.skip_skill:
        print("Skill install skipped (--skip-skill).")
    else:
        status = install_skill_symlink(
            checkout_root / "skills" / "decisions", home_dir / ".claude" / "skills" / "decisions",
        )
        print(f"Skill install: {status}")

    if args.skip_hook:
        print("Hook install skipped (--skip-hook).")
    else:
        settings_path = home_dir / ".claude" / "settings.json"
        # One backup for the whole batch below, taken before any of the seven
        # merge_hook calls touch the file — each call used to back up on its own,
        # leaving six redundant copies of the same pre-init state.
        if settings_path.exists():
            _backup(settings_path)
        merge_hook(
            settings_path, "SessionStart", str(checkout_root / "hooks" / "session-decisions-nudge.sh"),
            backup=False,
        )

        # Resolved to an absolute path when possible, same reasoning as
        # register_mcp_server's resolution: Claude Code execs these from its own
        # process, whose PATH may not match the shell `apo init` ran in.
        apo_bin = shutil.which("apo") or "apo"
        for event, subcommand, options in SESSION_HOOKS:
            merge_hook(settings_path, event, f"{apo_bin} {subcommand}", backup=False, **options)
        print("Session-tracking + nudge hooks installed.")

    print("\napo init complete.")
