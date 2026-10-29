"""Where apo keeps its data, and its settings.

Every setting is read when it's asked for, never at import: first from the
environment, then from the .env that `init` writes next to the data. A long-running
process or a test sees a change at once, with nothing to re-import.
"""
import os
from pathlib import Path

_FALSE = ("", "0", "false", "False", "no")


def data_dir() -> Path:
    return Path.home() / ".claude" / "ai-decision-orchestrator"


def home_env_path() -> Path:
    """Where `init` writes settings for an installed instance."""
    return data_dir() / ".env"


_file_cache: dict[Path, tuple[tuple[int, int], dict[str, str]]] = {}


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


def setting(name: str, default: str | None = None) -> str | None:
    """`name` from the environment, else from the .env file, else `default`. A name
    that is set but blank still counts, so a blank variable hides the file's value."""
    if name in os.environ:
        return os.environ[name]
    return read_env_file(home_env_path()).get(name, default)


def _flag(name: str) -> bool:
    return (setting(name) or "").strip() not in _FALSE


def _int(name: str, default: int) -> int:
    raw = setting(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def default_db_path() -> Path:
    return data_dir() / "orchestrator.db"


def get_db_path() -> Path:
    override = setting("ADO_DB")
    return Path(override).expanduser() if override else default_db_path()


def get_secret_key_path() -> Path:
    """Fernet key for credentials.py, stored alongside the DB (including an ADO_DB
    override in tests), so the key always travels with the data it protects."""
    return get_db_path().parent / "secret.key"


def get_attachments_dir() -> Path:
    """Decision-answer attachments (screenshots), stored alongside the DB."""
    return get_db_path().parent / "attachments"


def get_projects_root() -> Path:
    """The folder whose subfolders the dashboard offers to a new project, and the
    only place it will attach one from."""
    return Path(setting("ADO_PROJECTS_ROOT") or Path.home() / "projects").expanduser()


def offline() -> bool:
    """Kill switch for every outbound write to an external system. Set ADO_OFFLINE=1
    in any manual or scratch script that touches db.py: filing a decision pushes a
    real Linear issue and answering one comments on it and closes it. Enforced in
    linear_client._graphql (hard block) as well as LinearSource.is_configured."""
    return _flag("ADO_OFFLINE")


def dashboard_port() -> int:
    return _int("ADO_DASHBOARD_PORT", 8430)


def dashboard_autostart() -> bool:
    """Whether a Claude Code session starting brings the dashboard up when it's down:
    WSL has no systemd by default, so often nothing else would."""
    return _flag("ADO_DASHBOARD_AUTOSTART")


def linear_api_key() -> str | None:
    return setting("LINEAR_API_KEY")


def linear_team() -> str | None:
    """A team key (e.g. "ENG") or team id."""
    return setting("LINEAR_TEAM")


def poll_seconds() -> int:
    """The poll interval for every answer source, not just Linear."""
    return _int("POLL_SECONDS", _int("LINEAR_POLL_SECONDS", 30))


def sync_retry_max_attempts() -> int:
    """A failed Linear push or close is retried with growing gaps (see
    sources._RETRY_DELAYS), then abandoned, so a permanent failure doesn't hit the
    API every poll forever."""
    return _int("ADO_SYNC_RETRY_MAX_ATTEMPTS", 5)


def linear_followup_days() -> int:
    """How long after a decision is closed its Linear issue is still polled for
    follow-up comments. Open and answered-but-unworked decisions are always polled."""
    return _int("ADO_LINEAR_FOLLOWUP_DAYS", 14)


def claude_bin() -> str:
    return setting("ADO_CLAUDE_BIN") or "claude"


def claude_config_dirs() -> str:
    """Extra Claude Code profiles, separated like PATH."""
    return setting("ADO_CLAUDE_CONFIG_DIRS") or ""


def gh_bin() -> str:
    return setting("ADO_GH_BIN") or "gh"


def is_preview() -> bool:
    """A second dashboard run against a copy of the data says so on every page."""
    return _flag("ADO_PREVIEW")
