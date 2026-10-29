"""Shared configuration for the AI Decision Orchestrator. Loads local config from
.env (gitignored) into the environment."""
import os
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".claude" / "ai-decision-orchestrator" / "orchestrator.db"

# This maintainer's own dev convenience — config for whatever checkout is running.
_CHECKOUT_ENV_PATH = Path(__file__).parent / ".env"
# Where `ado init` writes config for an installed instance, so it isn't tied to any
# particular checkout.
HOME_ENV_PATH = DEFAULT_DB_PATH.parent / ".env"


def get_db_path() -> Path:
    override = os.environ.get("ADO_DB")
    return Path(override).expanduser() if override else DEFAULT_DB_PATH


def get_secret_key_path() -> Path:
    """Fernet key for credentials.py, stored alongside the DB (same directory,
    including an ADO_DB override in tests) rather than at a fixed location — an
    installed instance's key lives with its data, not tied to a particular checkout."""
    return get_db_path().parent / "secret.key"


def get_attachments_dir() -> Path:
    """Decision-answer attachments (screenshots), stored alongside the DB — same
    sibling-of-the-DB convention as get_secret_key_path()."""
    return get_db_path().parent / "attachments"


def get_projects_root() -> Path:
    """The folder whose subfolders the dashboard offers to a new project — and the
    only place it will attach one from."""
    return Path(os.environ.get("ADO_PROJECTS_ROOT") or Path.home() / "projects").expanduser()


def _apply_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _load_dotenv() -> None:
    """setdefault throughout means precedence is real env var > checkout .env >
    home .env — an installed instance's home-relative config never shadows a
    maintainer's own checkout-relative one, and neither ever overrides a real
    environment variable."""
    _apply_env_file(_CHECKOUT_ENV_PATH)
    _apply_env_file(HOME_ENV_PATH)


_load_dotenv()

# Kill switch for every outbound write to an external system. Set ADO_OFFLINE=1 in
# any manual or scratch script that touches db.py: filing a decision pushes a real
# Linear issue and answering one comments on it and closes it, so a verification
# script run against the real .env mutates the live workspace as a side effect of
# code that looks purely local. Enforced in linear_client._graphql (hard block) as
# well as LinearSource.is_configured (quiet skip).
OFFLINE = os.environ.get("ADO_OFFLINE", "").strip() not in ("", "0", "false", "False")

# The dashboard's port, and whether a Claude Code session starting brings it up when
# it's down: WSL has no systemd by default, so often nothing else would.
DASHBOARD_PORT = int(os.environ.get("ADO_DASHBOARD_PORT", "8430"))
DASHBOARD_AUTOSTART = os.environ.get("ADO_DASHBOARD_AUTOSTART", "").strip() not in ("", "0", "false", "False")

LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY")
LINEAR_TEAM = os.environ.get("LINEAR_TEAM")  # team key (e.g. "ENG") or team id
LINEAR_POLL_SECONDS = int(os.environ.get("LINEAR_POLL_SECONDS", "30"))
# Generic poll interval for all answer sources, not just Linear.
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", str(LINEAR_POLL_SECONDS)))
# How far back the dashboard's "recent sync errors" banner looks — a transient error
# from days ago shouldn't still read as a current problem. `ado sync-errors` ignores
# this and always shows full history.
SYNC_ERROR_BANNER_HOURS = int(os.environ.get("ADO_SYNC_ERROR_BANNER_HOURS", "24"))
# A failed Linear push/close is retried by the poll loop with growing gaps (see
# sources._RETRY_DELAYS), then abandoned — a permanent failure (a bug, a deleted
# issue) must not hit the API every POLL_SECONDS forever.
SYNC_RETRY_MAX_ATTEMPTS = int(os.environ.get("ADO_SYNC_RETRY_MAX_ATTEMPTS", "5"))
# How long after a decision is closed its Linear issue is still polled for follow-up
# comments. Open and answered-but-unworked decisions are always polled.
LINEAR_FOLLOWUP_DAYS = int(os.environ.get("ADO_LINEAR_FOLLOWUP_DAYS", "14"))
