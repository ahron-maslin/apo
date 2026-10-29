"""config.py's .env loading. An installed instance (via `ado init`) writes its config
to a home-relative .env next to the DB, rather than into whatever checkout it happens
to run from — this pins down the precedence between the two."""
import os
from pathlib import Path

import pytest


@pytest.fixture
def env_test_key():
    key = "SOME_ADO_TEST_KEY"
    os.environ.pop(key, None)
    yield key
    os.environ.pop(key, None)


def test_home_env_path_lives_alongside_the_default_db(env_test_key):
    from apo import config

    assert config.HOME_ENV_PATH == config.DEFAULT_DB_PATH.parent / ".env"


def test_apply_env_file_fills_an_unset_key(tmp_path, env_test_key):
    from apo import config

    env_file = tmp_path / ".env"
    env_file.write_text(f"{env_test_key}=from-file\n")

    config._apply_env_file(env_file)
    assert os.environ[env_test_key] == "from-file"


def test_apply_env_file_does_not_override_an_existing_env_var(tmp_path, env_test_key, monkeypatch):
    from apo import config

    monkeypatch.setenv(env_test_key, "from-real-env")
    env_file = tmp_path / ".env"
    env_file.write_text(f"{env_test_key}=from-file\n")

    config._apply_env_file(env_file)
    assert os.environ[env_test_key] == "from-real-env"


def test_load_dotenv_prefers_checkout_env_over_home_env(tmp_path, env_test_key, monkeypatch):
    from apo import config

    checkout_env = tmp_path / "checkout.env"
    checkout_env.write_text(f"{env_test_key}=from-checkout\n")
    home_env = tmp_path / "home.env"
    home_env.write_text(f"{env_test_key}=from-home\n")

    monkeypatch.setattr(config, "_CHECKOUT_ENV_PATH", checkout_env)
    monkeypatch.setattr(config, "HOME_ENV_PATH", home_env)

    config._load_dotenv()
    assert os.environ[env_test_key] == "from-checkout"


def test_get_secret_key_path_lives_alongside_the_db(db_path):
    from apo import config

    assert config.get_secret_key_path() == config.get_db_path().parent / "secret.key"


def test_get_attachments_dir_lives_alongside_the_db(db_path):
    from apo import config

    assert config.get_attachments_dir() == config.get_db_path().parent / "attachments"


def test_load_dotenv_falls_back_to_home_env_when_checkout_env_is_absent(tmp_path, env_test_key, monkeypatch):
    from apo import config

    home_env = tmp_path / "home.env"
    home_env.write_text(f"{env_test_key}=from-home\n")

    monkeypatch.setattr(config, "_CHECKOUT_ENV_PATH", tmp_path / "missing.env")
    monkeypatch.setattr(config, "HOME_ENV_PATH", home_env)

    config._load_dotenv()
    assert os.environ[env_test_key] == "from-home"


def test_projects_root_defaults_to_projects_in_home(monkeypatch):
    from apo import config

    monkeypatch.delenv("ADO_PROJECTS_ROOT", raising=False)
    assert config.get_projects_root() == Path.home() / "projects"


def test_projects_root_comes_from_the_environment(monkeypatch):
    from apo import config

    monkeypatch.setenv("ADO_PROJECTS_ROOT", "~/code")
    assert config.get_projects_root() == Path.home() / "code"
