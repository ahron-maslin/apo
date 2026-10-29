"""Where apo keeps its data, and its settings.

Every setting is read when it's asked for, never at import: first from the
environment, then from the settings file `apo init` writes (`config_file()`), then
from the .env an older install wrote into ~/.claude/ai-decision-orchestrator/. Each
setting is named APO_*, and still answers to the ADO_* name it had before the rename.
A long-running process or a test sees a change at once, with nothing to re-import.
"""
import os
import sys
from pathlib import Path

import platformdirs

APP_NAME = "apo"
LEGACY_NAME = "ai-decision-orchestrator"

_FALSE = ("", "0", "false", "False", "no")


def default_data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def legacy_data_dir() -> Path:
    return Path.home() / ".claude" / LEGACY_NAME


def data_dir() -> Path:
    """The platform data folder, unless an older install's
    ~/.claude/ai-decision-orchestrator exists and the new folder has no database yet:
    then the old one, so an existing install keeps its DB, key and attachments
    together. Nothing is ever moved. (On macOS the new folder doubles as the config
    folder, so it can exist without holding any data.)"""
    new = default_data_dir()
    legacy = legacy_data_dir()
    if legacy.is_dir() and not (new / "orchestrator.db").exists():
        return legacy
    return new


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def config_file() -> Path:
    """Where `apo init` saves settings."""
    return config_dir() / "config.env"


def legacy_env_file() -> Path:
    return legacy_data_dir() / ".env"


_file_cache: dict[Path, tuple[tuple[int, int], dict[str, str]]] = {}
_warned: set[str] = set()


def read_env_file(path: Path) -> dict[str, str]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _file_cache.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    values = {}
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    _file_cache[path] = (stamp, values)
    return values


def setting(name: str, default: str | None = None, bare: bool = False) -> str | None:
    """The first of APO_<name>, ADO_<name> and, with `bare`, plain <name> that is set:
    in the environment, then the settings file, then the old .env. A name that is set
    but blank still counts, so a blank APO_X hides every fallback."""
    names = (f"APO_{name}", f"ADO_{name}") + ((name,) if bare else ())
    for key in names:
        if key in os.environ:
            if key != names[0]:
                _deprecated(key, names[0])
            return os.environ[key]
    for path in (config_file(), legacy_env_file()):
        values = read_env_file(path)
        for key in names:
            if key in values:
                return values[key]
    return default


def _deprecated(old: str, new: str) -> None:
    """On stderr, never stdout: a hook's stdout is what Claude Code reads."""
    if old in _warned:
        return
    _warned.add(old)
    print(f"apo: {old} is deprecated; set {new} instead.", file=sys.stderr)


def _flag(name: str) -> bool:
    return (setting(name) or "").strip() not in _FALSE


def _int(name: str, default: int, bare: bool = False) -> int:
    raw = setting(name, bare=bare)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def default_db_path() -> Path:
    return data_dir() / "orchestrator.db"


def get_db_path() -> Path:
    override = setting("DB")
    return Path(override).expanduser() if override else default_db_path()


def get_secret_key_path() -> Path:
    """Fernet key for credentials.py, stored alongside the DB (including an APO_DB
    override in tests), so the key always travels with the data it protects."""
    return get_db_path().parent / "secret.key"


def get_attachments_dir() -> Path:
    """Decision-answer attachments (screenshots), stored alongside the DB."""
    return get_db_path().parent / "attachments"


def get_projects_root() -> Path:
    """The folder whose subfolders the dashboard offers to a new project, and the
    only place it will attach one from."""
    return Path(setting("PROJECTS_ROOT") or Path.home() / "projects").expanduser()


def offline() -> bool:
    """Kill switch for every outbound write to an external system. Set APO_OFFLINE=1
    in any manual or scratch script that touches db.py: filing a decision pushes a
    real Linear issue and answering one comments on it and closes it. Enforced in
    linear_client._graphql (hard block) as well as LinearSource.is_configured."""
    return _flag("OFFLINE")


def dashboard_port() -> int:
    return _int("DASHBOARD_PORT", 8430)


def dashboard_autostart() -> bool:
    """Whether a Claude Code session starting brings the dashboard up when it's down:
    WSL has no systemd by default, so often nothing else would."""
    return _flag("DASHBOARD_AUTOSTART")


def linear_api_key() -> str | None:
    return setting("LINEAR_API_KEY", bare=True)


def linear_team() -> str | None:
    """A team key (e.g. "ENG") or team id."""
    return setting("LINEAR_TEAM", bare=True)


def poll_seconds() -> int:
    """The poll interval for every answer source, not just Linear."""
    return _int("POLL_SECONDS", _int("LINEAR_POLL_SECONDS", 30, bare=True), bare=True)


def sync_retry_max_attempts() -> int:
    """A failed Linear push or close is retried with growing gaps (see
    sources._RETRY_DELAYS), then abandoned, so a permanent failure doesn't hit the
    API every poll forever."""
    return _int("SYNC_RETRY_MAX_ATTEMPTS", 5)


def linear_followup_days() -> int:
    """How long after a decision is closed its Linear issue is still polled for
    follow-up comments. Open and answered-but-unworked decisions are always polled."""
    return _int("LINEAR_FOLLOWUP_DAYS", 14)


def claude_bin() -> str:
    return setting("CLAUDE_BIN") or "claude"


def claude_config_dirs() -> str:
    """Extra Claude Code profiles, separated like PATH."""
    return setting("CLAUDE_CONFIG_DIRS") or ""


def gh_bin() -> str:
    return setting("GH_BIN") or "gh"


def is_preview() -> bool:
    """A second dashboard run against a copy of the data says so on every page."""
    return _flag("PREVIEW")
