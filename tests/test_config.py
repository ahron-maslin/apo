"""config.py reads every setting when it's asked for: the environment first, then the
settings file `apo init` writes, then the .env an older install left in
~/.claude/ai-decision-orchestrator/. Every setting is named APO_*, and still answers
to the ADO_* name it had before the rename. Nothing is cached at import, so a change
is seen at once and tests never need to re-import a module."""
from pathlib import Path

import platformdirs
import pytest

from apo import config


@pytest.fixture
def legacy():
    return Path.home() / ".claude" / "ai-decision-orchestrator"


@pytest.fixture
def settings_file(monkeypatch):
    for name in ("APO_DASHBOARD_PORT", "ADO_DASHBOARD_PORT"):
        monkeypatch.delenv(name, raising=False)
    path = config.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_a_setting_comes_from_the_environment_first(settings_file, monkeypatch):
    settings_file.write_text("APO_DASHBOARD_PORT=9000\n")
    monkeypatch.setenv("APO_DASHBOARD_PORT", "9100")

    assert config.dashboard_port() == 9100


def test_a_setting_falls_back_to_the_settings_file(settings_file):
    settings_file.write_text("# a comment\n\nAPO_DASHBOARD_PORT = 9000\n")

    assert config.dashboard_port() == 9000


def test_the_settings_file_lives_in_the_platform_config_folder():
    assert config.config_file() == Path(platformdirs.user_config_dir("apo", appauthor=False)) / "config.env"


def test_an_older_installs_env_file_still_counts(settings_file, legacy):
    legacy.mkdir(parents=True)
    (legacy / ".env").write_text("ADO_DASHBOARD_PORT=9300\n")

    assert config.dashboard_port() == 9300

    settings_file.write_text("APO_DASHBOARD_PORT=9000\n")
    assert config.dashboard_port() == 9000


def test_a_blank_variable_hides_the_files_value(settings_file, monkeypatch):
    settings_file.write_text("APO_LINEAR_API_KEY=from-file\n")
    monkeypatch.setenv("APO_LINEAR_API_KEY", "")

    assert not config.linear_api_key()


def test_a_change_is_seen_without_reimporting(settings_file, monkeypatch):
    monkeypatch.setenv("APO_OFFLINE", "1")
    assert config.offline()
    monkeypatch.setenv("APO_OFFLINE", "0")
    assert not config.offline()

    settings_file.write_text("APO_DASHBOARD_PORT=9000\n")
    assert config.dashboard_port() == 9000
    settings_file.write_text("APO_DASHBOARD_PORT=9001\n")
    assert config.dashboard_port() == 9001


def test_the_new_name_wins_over_the_old_one(monkeypatch):
    monkeypatch.setenv("APO_DASHBOARD_PORT", "9100")
    monkeypatch.setenv("ADO_DASHBOARD_PORT", "9200")

    assert config.dashboard_port() == 9100


@pytest.mark.parametrize("old, call, value, expected", [
    ("ADO_DASHBOARD_PORT", config.dashboard_port, "9200", 9200),
    ("ADO_OFFLINE", config.offline, "1", True),
    ("ADO_DASHBOARD_AUTOSTART", config.dashboard_autostart, "1", True),
    ("ADO_PROJECTS_ROOT", config.get_projects_root, "/srv/code", Path("/srv/code")),
    ("ADO_CLAUDE_BIN", config.claude_bin, "/opt/claude", "/opt/claude"),
    ("ADO_CLAUDE_CONFIG_DIRS", config.claude_config_dirs, "/a:/b", "/a:/b"),
    ("ADO_GH_BIN", config.gh_bin, "/opt/gh", "/opt/gh"),
    ("ADO_PREVIEW", config.is_preview, "1", True),
    ("ADO_SYNC_RETRY_MAX_ATTEMPTS", config.sync_retry_max_attempts, "7", 7),
    ("ADO_LINEAR_FOLLOWUP_DAYS", config.linear_followup_days, "3", 3),
])
def test_every_old_ado_name_still_works(monkeypatch, old, call, value, expected):
    monkeypatch.delenv("APO_" + old[4:], raising=False)
    monkeypatch.setenv(old, value)

    assert call() == expected


def test_the_old_db_name_still_works(monkeypatch, tmp_path):
    monkeypatch.delenv("APO_DB", raising=False)
    monkeypatch.setenv("ADO_DB", str(tmp_path / "old.db"))

    assert config.get_db_path() == tmp_path / "old.db"


def test_an_old_name_says_once_that_it_is_deprecated(monkeypatch, capsys):
    monkeypatch.delenv("APO_DASHBOARD_PORT", raising=False)
    monkeypatch.setenv("ADO_DASHBOARD_PORT", "9200")

    config.dashboard_port()
    config.dashboard_port()

    assert capsys.readouterr().err.splitlines() == [
        "apo: ADO_DASHBOARD_PORT is deprecated; set APO_DASHBOARD_PORT instead."]


@pytest.mark.parametrize("bare, call, value", [
    ("LINEAR_API_KEY", config.linear_api_key, "k"),
    ("LINEAR_TEAM", config.linear_team, "ENG"),
])
def test_the_bare_linear_names_still_work(monkeypatch, bare, call, value):
    monkeypatch.delenv("APO_" + bare, raising=False)
    monkeypatch.setenv(bare, value)

    assert call() == value


def test_poll_seconds_defaults_to_the_linear_interval(monkeypatch):
    for name in ("APO_POLL_SECONDS", "ADO_POLL_SECONDS", "POLL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APO_LINEAR_POLL_SECONDS", "45")

    assert config.poll_seconds() == 45


def test_a_bad_number_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("APO_DASHBOARD_PORT", "not-a-port")

    assert config.dashboard_port() == 8430


def test_a_new_install_keeps_its_data_in_the_platform_folder(monkeypatch):
    monkeypatch.delenv("APO_DB", raising=False)

    assert config.data_dir() == Path(platformdirs.user_data_dir("apo", appauthor=False))
    assert config.get_db_path() == config.data_dir() / "orchestrator.db"


def test_an_existing_install_keeps_using_its_old_folder(monkeypatch, legacy):
    monkeypatch.delenv("APO_DB", raising=False)
    legacy.mkdir(parents=True)
    (legacy / "orchestrator.db").write_bytes(b"")

    assert config.data_dir() == legacy
    assert config.get_db_path() == legacy / "orchestrator.db"
    assert config.get_secret_key_path() == legacy / "secret.key"
    assert [p.name for p in legacy.iterdir()] == ["orchestrator.db"]


def test_once_the_new_folder_exists_it_is_used(legacy):
    legacy.mkdir(parents=True)
    new = Path(platformdirs.user_data_dir("apo", appauthor=False))
    new.mkdir(parents=True)

    assert config.data_dir() == new


def test_get_secret_key_path_lives_alongside_the_db(db_path):
    assert config.get_secret_key_path() == config.get_db_path().parent / "secret.key"


def test_get_attachments_dir_lives_alongside_the_db(db_path):
    assert config.get_attachments_dir() == config.get_db_path().parent / "attachments"


def test_projects_root_defaults_to_projects_in_home(monkeypatch):
    monkeypatch.delenv("APO_PROJECTS_ROOT", raising=False)
    assert config.get_projects_root() == Path.home() / "projects"


def test_projects_root_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("APO_PROJECTS_ROOT", "~/code")
    assert config.get_projects_root() == Path.home() / "code"
