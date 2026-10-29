"""config.py reads every setting when it's asked for: the environment first, then the
.env that `init` writes next to the data. Nothing is cached at import, so a change is
seen at once and tests never need to re-import a module."""
from pathlib import Path

import pytest

from apo import config


@pytest.fixture
def home_env(tmp_path, monkeypatch):
    path = tmp_path / "home.env"
    monkeypatch.setattr(config, "home_env_path", lambda: path)
    return path


def test_home_env_path_lives_alongside_the_default_db():
    assert config.home_env_path() == config.default_db_path().parent / ".env"


def test_a_setting_comes_from_the_environment_first(home_env, monkeypatch):
    home_env.write_text("ADO_DASHBOARD_PORT=9000\n")
    monkeypatch.setenv("ADO_DASHBOARD_PORT", "9100")

    assert config.dashboard_port() == 9100


def test_a_setting_falls_back_to_the_env_file(home_env, monkeypatch):
    monkeypatch.delenv("ADO_DASHBOARD_PORT", raising=False)
    home_env.write_text("# a comment\n\nADO_DASHBOARD_PORT = 9000\n")

    assert config.dashboard_port() == 9000


def test_a_blank_variable_hides_the_files_value(home_env, monkeypatch):
    home_env.write_text("LINEAR_API_KEY=from-file\n")
    monkeypatch.setenv("LINEAR_API_KEY", "")

    assert not config.linear_api_key()


def test_a_change_is_seen_without_reimporting(home_env, monkeypatch):
    monkeypatch.setenv("ADO_OFFLINE", "1")
    assert config.offline()
    monkeypatch.setenv("ADO_OFFLINE", "0")
    assert not config.offline()

    monkeypatch.delenv("ADO_DASHBOARD_PORT", raising=False)
    home_env.write_text("ADO_DASHBOARD_PORT=9000\n")
    assert config.dashboard_port() == 9000
    home_env.write_text("ADO_DASHBOARD_PORT=9001\n")
    assert config.dashboard_port() == 9001


def test_a_bad_number_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("ADO_DASHBOARD_PORT", "not-a-port")

    assert config.dashboard_port() == 8430


def test_poll_seconds_defaults_to_the_linear_interval(monkeypatch):
    monkeypatch.delenv("POLL_SECONDS", raising=False)
    monkeypatch.setenv("LINEAR_POLL_SECONDS", "45")

    assert config.poll_seconds() == 45


def test_get_secret_key_path_lives_alongside_the_db(db_path):
    assert config.get_secret_key_path() == config.get_db_path().parent / "secret.key"


def test_get_attachments_dir_lives_alongside_the_db(db_path):
    assert config.get_attachments_dir() == config.get_db_path().parent / "attachments"


def test_projects_root_defaults_to_projects_in_home(monkeypatch):
    monkeypatch.delenv("ADO_PROJECTS_ROOT", raising=False)
    assert config.get_projects_root() == Path.home() / "projects"


def test_projects_root_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("ADO_PROJECTS_ROOT", "~/code")
    assert config.get_projects_root() == Path.home() / "code"
