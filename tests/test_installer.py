"""`apo init` sets up a new user's machine: the data folder, the MCP server `apo`, the
hooks that call the installed `apo`, and the `/decisions` skill, all in the Claude Code
profile it runs under. Run again, it replaces its own entries, including the ones an
older install left under the old names, instead of adding more.

Every test here runs against conftest's stand-in home and CLAUDE_CONFIG_DIR, and a
fake `claude`; nothing touches the real ~/.claude or ~/.claude.json."""
import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from apo import config, installer

APO = "/opt/apo tools/bin/apo"
REAL_HOME = Path(os.path.expanduser("~"))


@pytest.fixture(autouse=True)
def _never_the_real_claude_config():
    profile = Path(os.environ["CLAUDE_CONFIG_DIR"])
    assert Path.home() != REAL_HOME and REAL_HOME not in (profile, *profile.parents)


@pytest.fixture
def profile():
    return Path(os.environ["CLAUDE_CONFIG_DIR"])


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """A stand-in `claude` that records each call and succeeds."""
    log = tmp_path / "claude-calls.jsonl"
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\nimport json, sys\n"
                      f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    script.chmod(0o755)
    monkeypatch.setenv("APO_CLAUDE_BIN", str(script))
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture(autouse=True)
def _installed_apo(monkeypatch):
    monkeypatch.setattr(installer, "apo_executable", lambda: APO)


def _args(**overrides):
    return argparse.Namespace(**{
        "yes": True, "db_path": None, "projects_root": None, "linear_api_key": None, "linear_team": None,
        "skip_linear": True, "skip_mcp": False, "skip_hook": False, "skip_skill": False, **overrides,
    })


def _settings(profile):
    return json.loads((profile / "settings.json").read_text())


def _commands(settings, event):
    return [h["command"] for entry in settings.get("hooks", {}).get(event, []) for h in entry["hooks"]]


def test_init_registers_the_mcp_server_as_apo_at_user_scope(profile, claude):
    installer.run(_args())

    assert ["mcp", "add", "apo", "--scope", "user", "--", APO, "mcp"] in claude()


def test_init_replaces_an_old_registration_instead_of_adding_a_second(profile, claude):
    installer.run(_args())

    calls = claude()
    removes = [c for c in calls if c[:2] == ["mcp", "remove"]]
    assert ["mcp", "remove", "ai-decision-orchestrator", "--scope", "user"] in removes
    assert ["mcp", "remove", "apo", "--scope", "user"] in removes
    assert calls.index(removes[-1]) < calls.index(["mcp", "add", "apo", "--scope", "user", "--", APO, "mcp"])


def test_without_claude_init_says_how_to_register_by_hand(profile, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APO_CLAUDE_BIN", str(tmp_path / "no-claude-here"))

    installer.run(_args())

    out = capsys.readouterr().out
    assert "claude mcp add apo --scope user --" in out and "mcp" in out


def test_init_adds_hooks_that_call_the_installed_apo(profile, claude):
    installer.run(_args())

    settings = _settings(profile)
    quoted = "'/opt/apo tools/bin/apo'"
    for event, subcommand, _ in installer.SESSION_HOOKS:
        assert f"{quoted} {subcommand}" in _commands(settings, event), event
    [nudge] = [c for c in _commands(settings, "SessionStart") if c.endswith("session-decisions-nudge.sh")]
    assert Path(nudge).read_text() == installer.packaged("hooks/session-decisions-nudge.sh").read_text()
    assert os.access(nudge, os.X_OK)


def test_the_hooks_keep_their_timing(profile, claude):
    installer.run(_args())

    hooks = _settings(profile)["hooks"]
    heartbeat = hooks["PostToolUse"][0]["hooks"][0]
    assert heartbeat["async"] is True
    stop = hooks["Stop"][0]["hooks"][0]
    assert "async" not in stop and stop["timeout"] <= 5
    permission = hooks["PermissionRequest"][0]["hooks"][0]
    assert "async" not in permission and permission["timeout"] > 3600
    assert hooks["PreToolUse"][0]["matcher"] == "AskUserQuestion|ExitPlanMode"


def test_running_init_twice_adds_nothing_the_second_time(profile, claude):
    installer.run(_args())
    first = _settings(profile)
    installer.run(_args())

    assert _settings(profile) == first
    for event, _, _ in installer.SESSION_HOOKS:
        assert len(_commands(first, event)) == (2 if event == "SessionStart" else 1), event


def test_init_replaces_an_older_installs_hooks_and_keeps_everyone_elses(profile, claude):
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "settings.json").write_text(json.dumps({
        "model": "opus",
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "/old/checkout/hooks/session-decisions-nudge.sh"}]},
                {"hooks": [{"type": "command", "command": "/old/venv/bin/ado session-start"}]},
                {"hooks": [{"type": "command", "command": "~/bin/my-own-hook.sh"}]},
            ],
            "PostToolUse": [{"hooks": [{"type": "command", "command": "/old/venv/bin/ado session-heartbeat", "async": True}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "/usr/local/bin/apo session-stop", "timeout": 2},
                                {"type": "command", "command": "say done"}]}],
        },
    }))

    installer.run(_args())

    settings = _settings(profile)
    assert settings["model"] == "opus"
    everything = [c for event in settings["hooks"] for c in _commands(settings, event)]
    assert not any("/old/" in c or "/usr/local/bin/apo" in c for c in everything)
    assert "~/bin/my-own-hook.sh" in everything and "say done" in everything
    assert _commands(settings, "Stop").count("'/opt/apo tools/bin/apo' session-stop") == 1


def test_settings_are_backed_up_once_and_only_when_init_changes_them(profile, claude):
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "settings.json").write_text('{"model": "opus"}')

    installer.run(_args())
    installer.run(_args())

    [backup] = profile.glob("settings.json.bak-*")
    assert json.loads(backup.read_text()) == {"model": "opus"}


def test_a_settings_file_that_isnt_json_is_left_alone(profile, claude, capsys):
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "settings.json").write_text("{ not json")

    installer.run(_args())

    assert (profile / "settings.json").read_text() == "{ not json"
    assert "settings.json" in capsys.readouterr().out


def test_permissions_under_the_old_server_name_move_to_the_new_one(profile, claude):
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "settings.json").write_text(json.dumps({"permissions": {"allow": [
        "mcp__ai-decision-orchestrator__create_decision_request", "mcp__ai-decision-orchestrator", "Bash(ls:*)",
        "mcp__apo__list_tasks",
    ]}}))

    installer.run(_args())

    assert _settings(profile)["permissions"]["allow"] == [
        "mcp__apo__create_decision_request", "mcp__apo", "Bash(ls:*)", "mcp__apo__list_tasks"]


def test_init_installs_the_decisions_skill_as_a_copy(profile, claude):
    installer.run(_args())

    skill = profile / "skills" / "decisions" / "SKILL.md"
    assert skill.is_file() and not skill.parent.is_symlink()
    text = skill.read_text()
    assert text.startswith("---\nname: decisions\n")
    assert "Installed by apo" in text
    assert installer.packaged("skills/decisions/SKILL.md").read_text().split("---", 2)[2].strip() in text


def test_init_replaces_the_old_installs_skill_link(profile, claude, tmp_path):
    old = tmp_path / "old-checkout" / "skills" / "decisions"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("---\nname: decisions\n---\nWork the rows in .claude/decisions/INDEX.md\n")
    (profile / "skills").mkdir(parents=True)
    (profile / "skills" / "decisions").symlink_to(old, target_is_directory=True)

    installer.run(_args())

    skill = profile / "skills" / "decisions"
    assert not skill.is_symlink() and "Installed by apo" in (skill / "SKILL.md").read_text()
    assert (old / "SKILL.md").exists()


def test_init_never_overwrites_someone_elses_decisions_skill(profile, claude, capsys):
    mine = profile / "skills" / "decisions"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own skill")

    installer.run(_args())

    assert (mine / "SKILL.md").read_text() == "my own skill"
    assert "skipped" in capsys.readouterr().out.lower()


def test_init_creates_a_private_data_folder(claude, monkeypatch):
    monkeypatch.delenv("APO_DB", raising=False)

    installer.run(_args())

    data = config.data_dir()
    assert data == config.default_data_dir() and data.is_dir()
    assert stat.S_IMODE(data.stat().st_mode) == 0o700


def test_init_keeps_an_older_installs_data_folder_where_it_is(claude, monkeypatch):
    monkeypatch.delenv("APO_DB", raising=False)
    legacy = config.legacy_data_dir()
    legacy.mkdir(parents=True)
    (legacy / "orchestrator.db").write_bytes(b"data")

    installer.run(_args())

    assert not config.default_data_dir().exists()
    assert (legacy / "orchestrator.db").read_bytes() == b"data"


def test_the_answers_are_saved_to_a_private_settings_file(claude, tmp_path):
    installer.run(_args(projects_root=str(tmp_path / "code"), db_path=str(tmp_path / "apo.db"),
                        skip_linear=False, linear_api_key="lin-key", linear_team="ENG"))

    settings = config.read_env_file(config.config_file())
    assert settings == {"APO_PROJECTS_ROOT": str(tmp_path / "code"), "APO_DB": str(tmp_path / "apo.db"),
                        "APO_LINEAR_API_KEY": "lin-key", "APO_LINEAR_TEAM": "ENG"}
    assert stat.S_IMODE(config.config_file().stat().st_mode) == 0o600
    assert (tmp_path / "code").is_dir()


def test_init_asks_where_projects_live_and_saves_it(claude, tmp_path, monkeypatch):
    prompts, answers = [], iter(["", str(tmp_path / "code"), "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or next(answers))

    installer.run(_args(yes=False, skip_linear=False))

    assert any(p.startswith("Where do your projects live?") for p in prompts)
    assert config.read_env_file(config.config_file())["APO_PROJECTS_ROOT"] == str(tmp_path / "code")


def test_pressing_enter_keeps_the_projects_folder_it_has(claude, tmp_path, monkeypatch):
    monkeypatch.setenv("APO_PROJECTS_ROOT", str(tmp_path / "already"))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")

    installer.run(_args(yes=False))

    [asked] = [p for p in prompts if p.startswith("Where do your projects live?")]
    assert str(tmp_path / "already") in asked
    assert "APO_PROJECTS_ROOT" not in config.read_env_file(config.config_file())


def test_skip_flags_leave_claude_code_alone(profile, claude):
    installer.run(_args(skip_mcp=True, skip_hook=True, skip_skill=True))

    assert claude() == []
    assert not (profile / "settings.json").exists() and not (profile / "skills").exists()


def test_init_ends_with_what_to_do_next(claude, capsys):
    installer.run(_args())

    out = capsys.readouterr().out
    assert "new Claude Code session" in out and "apo dashboard start" in out
    assert str(config.data_dir()) in out


def test_apo_init_from_the_command_line(tmp_path, profile, claude):
    """The whole command, as a new user runs it, in a stand-in home."""
    env = dict(os.environ, PATH=f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}")
    env.pop("APO_DB", None)

    result = subprocess.run([sys.executable, "-m", "apo", "init", "--yes"], capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr
    apo_bin = str(Path(sys.executable).parent / "apo")
    assert ["mcp", "add", "apo", "--scope", "user", "--", apo_bin, "mcp"] in claude()
    assert f"{apo_bin} session-start" in _commands(_settings(profile), "SessionStart")
    assert (profile / "skills" / "decisions" / "SKILL.md").is_file()


def test_write_env_updates_creates_file_with_given_keys(tmp_path):
    env_path = tmp_path / ".env"
    installer.write_env_updates(env_path, {"APO_DB": "/x/y.db"})

    assert env_path.read_text() == "APO_DB=/x/y.db\n"


def test_write_env_updates_preserves_existing_unrelated_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("APO_LINEAR_API_KEY=abc\nAPO_LINEAR_TEAM=ENG\n")

    installer.write_env_updates(env_path, {"APO_DB": "/x/y.db"})

    content = env_path.read_text()
    assert "APO_LINEAR_API_KEY=abc" in content
    assert "APO_LINEAR_TEAM=ENG" in content
    assert "APO_DB=/x/y.db" in content


def test_write_env_updates_overwrites_an_existing_key(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("APO_DB=/old.db\n")

    installer.write_env_updates(env_path, {"APO_DB": "/new.db"})

    content = env_path.read_text()
    assert "APO_DB=/new.db" in content
    assert "/old.db" not in content
