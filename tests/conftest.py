import os
import time
import urllib.request
from zoneinfo import ZoneInfo

import pytest

from apo import changelog, config, credentials
from apo.integrations import linear_client, sources
from apo.web import app


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
    .claude/decisions/ here, in apo's own source tree, mixing test
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
    monkeypatch.setenv("APO_CLAUDE_BIN", str(tmp_path / "no-such-claude"))
    monkeypatch.setenv("APO_GH_BIN", str(tmp_path / "no-such-gh"))


@pytest.fixture(autouse=True)
def _never_read_the_real_home(tmp_path, monkeypatch):
    """Fourth backstop. Settings and the data folder live under the home folder, so
    without this a test, or a subprocess it starts, would read this machine's real
    .env (a real Linear key among them) and could write into the real data folder or
    Claude Code config. The stand-in home is a sibling of tmp_path, not its parent, so
    tmp_path stays an ordinary folder rather than "home or above"."""
    home = tmp_path.parent / f"{tmp_path.name}-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fresh_module_state():
    """What a long-running process keeps in memory between requests, reset so no test
    sees another's: Linear ids resolved under some credential, cached pull requests,
    when a fleet page was last viewed, and which old
    setting names have already been warned about."""
    linear_client.clear_caches()
    config._warned.clear()
    changelog._pr_cache.clear()
    app._last_viewed_at = 0.0
    app._stopping = False


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """DB isolation *and* API isolation: a fresh DB, and APO_LINEAR_API_KEY/APO_LINEAR_TEAM
    set blank rather than deleted, since a blank variable hides every fallback (the
    old names, the settings file, an old .env)."""
    path = tmp_path / "orchestrator.db"
    monkeypatch.setenv("APO_DB", str(path))
    monkeypatch.setenv("APO_LINEAR_API_KEY", "")
    monkeypatch.setenv("APO_LINEAR_TEAM", "")
    # Tests run inside a live Claude Code session, which sets this for real — blank it
    # so origin_session_id auto-detection tests are explicit about what they're testing
    # rather than silently picking up whatever session is actually running the suite.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return path


@pytest.fixture
def config_module(db_path):
    return config


@pytest.fixture
def credentials_module(db_path):
    return credentials


@pytest.fixture
def linear_client_module(db_path):
    return linear_client


@pytest.fixture
def sources_module(db_path):
    return sources


@pytest.fixture
def local_zone():
    """Pin the machine's local time zone (what naive datetimes and .astimezone() mean)
    for one test, so a test about a clock change doesn't depend on where it runs.
    Call it with a zone name; it returns that ZoneInfo."""
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset, which Windows doesn't have")
    saved = os.environ.get("TZ")

    def pin(name: str) -> ZoneInfo:
        os.environ["TZ"] = name
        time.tzset()
        return ZoneInfo(name)

    yield pin
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()
