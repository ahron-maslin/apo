"""config.py reads every setting when it's asked for: the environment first, then the
settings file `apo init` writes. Every setting is named APO_*; the ADO_* names and the
~/.claude/ai-decision-orchestrator folder from before the rename are no longer read.
Nothing is cached at import, so a change is seen at once and tests never need to
re-import a module."""
from pathlib import Path

import platformdirs
import pytest

from apo import config


@pytest.fixture
def legacy():
    return Path.home() / ".claude" / "ai-decision-orchestrator"


@pytest.fixture
def settings_file(monkeypatch):
    monkeypatch.delenv("APO_DASHBOARD_PORT", raising=False)
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


def test_an_old_installs_env_file_is_not_read(settings_file, legacy):
    legacy.mkdir(parents=True)
    (legacy / ".env").write_text("APO_DASHBOARD_PORT=9300\nADO_DASHBOARD_PORT=9301\n")

    assert config.dashboard_port() == 8430


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


@pytest.mark.parametrize("old, call, default", [
    ("ADO_DASHBOARD_PORT", config.dashboard_port, 8430),
    ("ADO_OFFLINE", config.offline, False),
    ("ADO_LINEAR_FOLLOWUP_DAYS", config.linear_followup_days, None),
])
def test_an_old_ado_name_is_not_read(monkeypatch, capsys, old, call, default):
    monkeypatch.delenv("APO_" + old[4:], raising=False)
    expected = call() if default is None else default
    monkeypatch.setenv(old, "7")

    assert call() == expected
    assert capsys.readouterr().err == ""


def test_the_old_db_name_is_not_read(monkeypatch, tmp_path):
    monkeypatch.delenv("APO_DB", raising=False)
    monkeypatch.setenv("ADO_DB", str(tmp_path / "old.db"))

    assert config.get_db_path() == config.default_data_dir() / "orchestrator.db"


@pytest.mark.parametrize("bare, call, value", [
    ("LINEAR_API_KEY", config.linear_api_key, "k"),
    ("LINEAR_TEAM", config.linear_team, "ENG"),
])
def test_the_bare_linear_names_still_work(monkeypatch, bare, call, value):
    monkeypatch.delenv("APO_" + bare, raising=False)
    monkeypatch.setenv(bare, value)

    assert call() == value


def test_poll_seconds_defaults_to_the_linear_interval(monkeypatch):
    for name in ("APO_POLL_SECONDS", "POLL_SECONDS"):
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


def test_an_old_ado_data_folder_is_not_used(monkeypatch, legacy):
    monkeypatch.delenv("APO_DB", raising=False)
    legacy.mkdir(parents=True)
    (legacy / "orchestrator.db").write_bytes(b"")

    assert config.data_dir() == Path(platformdirs.user_data_dir("apo", appauthor=False))
    assert config.get_db_path() == config.data_dir() / "orchestrator.db"
