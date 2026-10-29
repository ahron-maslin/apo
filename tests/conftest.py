import importlib
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _block_real_network_calls(monkeypatch):
    """Hard backstop, not just a convention to remember: linear_client._graphql is
    the only thing in this codebase that calls urllib.request.urlopen, so if this
    fires during a test, some code path reached the live Linear API without being
    mocked — e.g. answering a decision triggers mark_resolved -> move_to_state, which
    is easy to forget to mock in a test that's nominally about something else (this
    caught exactly that while writing the Phase 3 test suite: a test with a
    fake-but-truthy LINEAR_API_KEY only 401'd instead of silently mutating a real
    Linear workspace, purely by luck of the key being invalid rather than real)."""
    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "Test attempted a real network call via urllib.request.urlopen — mock "
            "the relevant linear_client function(s) instead of hitting the live API."
        )
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


@pytest.fixture(autouse=True)
def _never_write_into_the_real_checkout(tmp_path, monkeypatch):
    """Second hard backstop, same reasoning as the network block above.

    Filing a decision auto-registers a repository from cwd when its project has none
    (db.ensure_repository_for_project), and every state change then writes markdown
    into that repository (repo_writer). Under pytest, cwd is this checkout — so a test
    that just calls create_decision_request without thinking about it would create
    .claude/decisions/ here, in the orchestrator's own source tree, mixing test
    fixtures into real files. Forcing cwd somewhere disposable makes that structurally
    impossible instead of something each test has to remember."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _never_read_the_real_claude_runtime(tmp_path, monkeypatch):
    """Third backstop. The suite runs inside a live Claude Code session, so without
    this, session resolution (claude_runtime) would read this machine's real
    ~/.claude/sessions records or shell out to the real `claude agents` — tests would
    silently depend on whatever sessions happen to be open."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("ADO_CLAUDE_BIN", str(tmp_path / "no-such-claude"))
    monkeypatch.setenv("ADO_GH_BIN", str(tmp_path / "no-such-gh"))


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """DB isolation *and* API isolation.

    config.py's _load_dotenv() uses os.environ.setdefault(), so it won't override an
    already-set var — setting LINEAR_API_KEY/LINEAR_TEAM to "" (not deleting them)
    is what actually blocks _load_dotenv() from repopulating them from a real .env.
    And since config.py reads them into module-level constants at import time, the
    env vars alone aren't enough — already-imported modules must be re-imported so
    those constants get re-evaluated against the blanked environment.
    """
    path = tmp_path / "orchestrator.db"
    monkeypatch.setenv("ADO_DB", str(path))
    monkeypatch.setenv("LINEAR_API_KEY", "")
    monkeypatch.setenv("LINEAR_TEAM", "")
    # Tests run inside a live Claude Code session, which sets this for real — blank it
    # so origin_session_id auto-detection tests are explicit about what they're testing
    # rather than silently picking up whatever session is actually running the suite.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    for mod in (
        "db", "config", "credentials", "linear_client", "linear_goals", "app", "sources", "repo_writer",
        "mcp_server", "claude_runtime", "changelog",
    ):
        sys.modules.pop(mod, None)
    return path


@pytest.fixture
def config_module(db_path):
    """Freshly-imported config, so LINEAR_API_KEY/LINEAR_TEAM reflect this test's
    blanked environment rather than whatever was cached at collection time. Only
    needed by tests that check Linear-configured-vs-not — see db_path's docstring."""
    return importlib.import_module("apo.config")


@pytest.fixture
def credentials_module(db_path):
    return importlib.import_module("apo.credentials")


@pytest.fixture
def linear_client_module(db_path):
    return importlib.import_module("apo.integrations.linear_client")


@pytest.fixture
def sources_module(db_path):
    return importlib.import_module("apo.integrations.sources")
