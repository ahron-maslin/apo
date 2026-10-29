"""`ado init` sets up a teammate's own machine: DB location, optional Linear config,
MCP server registration, and installing the hook + skill into their Claude Code
config. It's the riskiest code in the packaging work — it mutates a file the
teammate's whole Claude Code setup depends on and shells out to an external binary —
so every piece is tested in isolation, entirely inside tmp_path, and the `claude`
subprocess call is always mocked.
"""
import argparse
import json

import pytest


def test_write_env_updates_creates_file_with_given_keys(tmp_path):
    from apo import installer

    env_path = tmp_path / ".env"
    installer.write_env_updates(env_path, {"ADO_DB": "/x/y.db"})

    assert env_path.read_text() == "ADO_DB=/x/y.db\n"


def test_write_env_updates_preserves_existing_unrelated_keys(tmp_path):
    from apo import installer

    env_path = tmp_path / ".env"
    env_path.write_text("LINEAR_API_KEY=abc\nLINEAR_TEAM=ENG\n")

    installer.write_env_updates(env_path, {"ADO_DB": "/x/y.db"})

    content = env_path.read_text()
    assert "LINEAR_API_KEY=abc" in content
    assert "LINEAR_TEAM=ENG" in content
    assert "ADO_DB=/x/y.db" in content


def test_write_env_updates_overwrites_an_existing_key(tmp_path):
    from apo import installer

    env_path = tmp_path / ".env"
    env_path.write_text("ADO_DB=/old.db\n")

    installer.write_env_updates(env_path, {"ADO_DB": "/new.db"})

    content = env_path.read_text()
    assert "ADO_DB=/new.db" in content
    assert "/old.db" not in content


def test_install_skill_symlink_creates_a_new_symlink(tmp_path):
    from apo import installer

    source = tmp_path / "checkout" / "skills" / "decisions"
    source.mkdir(parents=True)
    dest = tmp_path / "claude-home" / "skills" / "decisions"

    status = installer.install_skill_symlink(source, dest)

    assert status == "created"
    assert dest.is_symlink()
    assert dest.resolve() == source.resolve()


def test_install_skill_symlink_is_idempotent(tmp_path):
    from apo import installer

    source = tmp_path / "checkout" / "skills" / "decisions"
    source.mkdir(parents=True)
    dest = tmp_path / "claude-home" / "skills" / "decisions"
    installer.install_skill_symlink(source, dest)

    status = installer.install_skill_symlink(source, dest)

    assert status == "already-linked"


def test_install_skill_symlink_does_not_clobber_a_foreign_path(tmp_path):
    from apo import installer

    source = tmp_path / "checkout" / "skills" / "decisions"
    source.mkdir(parents=True)
    dest = tmp_path / "claude-home" / "skills" / "decisions"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("someone else's skill")

    status = installer.install_skill_symlink(source, dest)

    assert status == "skipped-exists"
    assert (dest / "SKILL.md").read_text() == "someone else's skill"


def test_merge_hook_creates_settings_file_when_absent(tmp_path):
    from apo import installer

    settings_path = tmp_path / "settings.json"
    installer.merge_hook(settings_path, "SessionStart", "/path/to/hook.sh")

    data = json.loads(settings_path.read_text())
    commands = [h["command"] for entry in data["hooks"]["SessionStart"] for h in entry["hooks"]]
    assert "/path/to/hook.sh" in commands


def test_merge_hook_preserves_unrelated_hooks(tmp_path):
    from apo import installer

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "/other/hook.sh"}]}]},
        "someOtherSetting": True,
    }))

    installer.merge_hook(settings_path, "SessionStart", "/path/to/hook.sh")

    data = json.loads(settings_path.read_text())
    commands = [h["command"] for entry in data["hooks"]["SessionStart"] for h in entry["hooks"]]
    assert "/other/hook.sh" in commands
    assert "/path/to/hook.sh" in commands
    assert data["someOtherSetting"] is True


def test_merge_hook_is_idempotent(tmp_path):
    from apo import installer

    settings_path = tmp_path / "settings.json"
    installer.merge_hook(settings_path, "SessionStart", "/path/to/hook.sh")
    installer.merge_hook(settings_path, "SessionStart", "/path/to/hook.sh")

    data = json.loads(settings_path.read_text())
    commands = [h["command"] for entry in data["hooks"]["SessionStart"] for h in entry["hooks"]]
    assert commands.count("/path/to/hook.sh") == 1


def test_merge_hook_backs_up_existing_file(tmp_path):
    from apo import installer

    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"hooks": {}}')

    installer.merge_hook(settings_path, "SessionStart", "/path/to/hook.sh")

    backups = list(tmp_path.glob("settings.json.bak-*"))
    assert len(backups) == 1


def test_merge_hook_works_for_any_event_independently(tmp_path):
    from apo import installer

    settings_path = tmp_path / "settings.json"
    installer.merge_hook(settings_path, "PostToolUse", "ado session-heartbeat")
    installer.merge_hook(settings_path, "Stop", "ado session-stop")

    data = json.loads(settings_path.read_text())
    assert data["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "ado session-heartbeat"
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "ado session-stop"
    # Each event's array is independent — adding Stop must not touch PostToolUse's.
    assert len(data["hooks"]["PostToolUse"]) == 1


def test_merge_hook_supports_an_optional_timeout(tmp_path):
    from apo import installer

    settings_path = tmp_path / "settings.json"
    installer.merge_hook(settings_path, "Stop", "ado session-stop", timeout=2)

    data = json.loads(settings_path.read_text())
    hook_entry = data["hooks"]["Stop"][0]["hooks"][0]
    assert hook_entry["command"] == "ado session-stop"
    assert hook_entry["timeout"] == 2


def test_merge_hook_can_mark_a_hook_async(tmp_path):
    from apo import installer

    settings_path = tmp_path / "settings.json"
    installer.merge_hook(settings_path, "PostToolUse", "ado session-heartbeat", run_async=True)

    hook_entry = json.loads(settings_path.read_text())["hooks"]["PostToolUse"][0]["hooks"][0]
    assert hook_entry["async"] is True


def test_merge_hook_rerun_updates_an_existing_entry_in_place(tmp_path):
    """An install from before async existed must pick it up on the next `ado init`
    without duplicating the entry."""
    from apo import installer

    settings_path = tmp_path / "settings.json"
    installer.merge_hook(settings_path, "PostToolUse", "ado session-heartbeat")
    installer.merge_hook(settings_path, "PostToolUse", "ado session-heartbeat", run_async=True)

    bucket = json.loads(settings_path.read_text())["hooks"]["PostToolUse"]
    assert len(bucket) == 1
    assert bucket[0]["hooks"][0]["async"] is True


def test_merge_hook_rerun_can_turn_async_back_off(tmp_path):
    from apo import installer

    settings_path = tmp_path / "settings.json"
    installer.merge_hook(settings_path, "Stop", "ado session-stop", run_async=True)
    installer.merge_hook(settings_path, "Stop", "ado session-stop", timeout=2)

    hook_entry = json.loads(settings_path.read_text())["hooks"]["Stop"][0]["hooks"][0]
    assert "async" not in hook_entry
    assert hook_entry["timeout"] == 2


def test_merge_hook_can_scope_a_hook_to_a_tool_matcher(tmp_path):
    from apo import installer

    settings_path = tmp_path / "settings.json"
    installer.merge_hook(settings_path, "PreToolUse", "ado session-pretool", matcher="AskUserQuestion|ExitPlanMode")
    installer.merge_hook(settings_path, "PreToolUse", "ado session-pretool", matcher="AskUserQuestion|ExitPlanMode")

    bucket = json.loads(settings_path.read_text())["hooks"]["PreToolUse"]
    assert len(bucket) == 1
    assert bucket[0]["matcher"] == "AskUserQuestion|ExitPlanMode"


def test_run_installs_the_waiting_state_hooks(tmp_path, monkeypatch):
    from apo import installer

    checkout = tmp_path / "checkout"
    (checkout / "skills" / "decisions").mkdir(parents=True)
    (checkout / "hooks").mkdir(parents=True)
    (checkout / "hooks" / "session-decisions-nudge.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(installer, "__file__", str(checkout / "installer.py"))
    home = tmp_path / "home"
    args = argparse.Namespace(
        yes=True, projects_root=None, db_path=None, linear_api_key=None, linear_team=None,
        skip_linear=True, skip_mcp=True, skip_hook=False, skip_skill=True,
    )

    installer.run(args, home_dir=home)

    hooks = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]
    for event, subcommand in (("Notification", "session-notification"), ("StopFailure", "session-stop-failure")):
        entry = hooks[event][0]
        assert entry["hooks"][0]["command"].endswith(subcommand)
        assert entry["hooks"][0]["async"] is True, event
    # These hold a background session's prompt for the dashboard and return its answer,
    # so they can't be async (Claude Code ignores async output), and must outlast the hold.
    for event, subcommand in (("PermissionRequest", "session-permission"), ("PreToolUse", "session-pretool")):
        hook = hooks[event][0]["hooks"][0]
        assert hook["command"].endswith(subcommand)
        assert "async" not in hook and hook["timeout"] > 3600, event
    # Only the two tools that mean "waiting on the human" — not a process per tool call.
    assert hooks["PreToolUse"][0]["matcher"] == "AskUserQuestion|ExitPlanMode"
    # The prompt hook's output carries answers back to the asking session, so it can't
    # be async (Claude Code ignores async output); it gets a short timeout instead.
    prompt_hook = hooks["UserPromptSubmit"][0]["hooks"][0]
    assert prompt_hook["command"].endswith("session-prompt")
    assert "async" not in prompt_hook and prompt_hook["timeout"] <= 5


def test_register_mcp_server_invokes_claude_cli_with_resolved_ado_mcp_path(monkeypatch):
    from apo import installer

    calls = []
    which_targets = {"claude": "/usr/bin/claude", "ado-mcp": "/venv/bin/ado-mcp"}
    monkeypatch.setattr(installer.shutil, "which", lambda name: which_targets.get(name))

    class FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    ok, message = installer.register_mcp_server()

    assert ok is True
    assert calls == [
        ["claude", "mcp", "add", "ai-decision-orchestrator", "--scope", "user", "--", "/venv/bin/ado-mcp"]
    ]


def test_register_mcp_server_falls_back_to_bare_name_when_ado_mcp_is_unresolvable(monkeypatch):
    from apo import installer

    calls = []
    monkeypatch.setattr(
        installer.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    class FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    installer.register_mcp_server()

    assert calls == [["claude", "mcp", "add", "ai-decision-orchestrator", "--scope", "user", "--", "ado-mcp"]]


def test_register_mcp_server_reports_missing_claude_cli_without_crashing(monkeypatch):
    from apo import installer

    monkeypatch.setattr(installer.shutil, "which", lambda name: None)

    ok, message = installer.register_mcp_server()

    assert ok is False
    assert "claude" in message.lower()


def test_register_mcp_server_surfaces_a_nonzero_exit_without_raising(monkeypatch):
    from apo import installer

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/claude")

    class FakeResult:
        returncode = 1
        stderr = "already registered"

    monkeypatch.setattr(installer.subprocess, "run", lambda cmd, **kwargs: FakeResult())

    ok, message = installer.register_mcp_server()

    assert ok is False
    assert "already registered" in message


def test_run_end_to_end_with_yes_and_explicit_values(tmp_path, monkeypatch):
    from apo import installer

    checkout = tmp_path / "checkout"
    (checkout / "skills" / "decisions").mkdir(parents=True)
    (checkout / "hooks").mkdir(parents=True)
    (checkout / "hooks" / "session-decisions-nudge.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(installer, "__file__", str(checkout / "installer.py"))
    monkeypatch.setattr(installer, "register_mcp_server", lambda *a, **k: (True, "Registered."))

    home = tmp_path / "home"
    args = argparse.Namespace(
        yes=True, projects_root=None, db_path=str(tmp_path / "custom.db"),
        linear_api_key="key123", linear_team="ENG",
        skip_linear=False, skip_mcp=False, skip_hook=False, skip_skill=False,
    )

    installer.run(args, home_dir=home)

    env_content = (home / ".claude" / "ai-decision-orchestrator" / ".env").read_text()
    assert "custom.db" in env_content
    assert "LINEAR_API_KEY=key123" in env_content
    assert "LINEAR_TEAM=ENG" in env_content

    assert (home / ".claude" / "skills" / "decisions").is_symlink()

    settings = json.loads((home / ".claude" / "settings.json").read_text())

    def commands_for(event):
        return [h["command"] for entry in settings["hooks"].get(event, []) for h in entry["hooks"]]

    session_start_commands = commands_for("SessionStart")
    assert any("session-decisions-nudge.sh" in c for c in session_start_commands)
    assert any(c.endswith("session-start") for c in session_start_commands)
    assert any(c.endswith("session-heartbeat") for c in commands_for("PostToolUse"))
    assert any(c.endswith("session-stop") for c in commands_for("Stop"))
    assert any(c.endswith("session-end") for c in commands_for("SessionEnd"))
    assert any(c.endswith("checklist-item-created") for c in commands_for("TaskCreated"))
    assert any(c.endswith("checklist-item-completed") for c in commands_for("TaskCompleted"))

    # Stop can block the agent's turn if it hangs — must carry a short timeout.
    stop_entry = settings["hooks"]["Stop"][0]["hooks"][0]
    assert stop_entry.get("timeout") is not None and stop_entry["timeout"] <= 5
    assert "async" not in stop_entry

    # The heartbeat fires on every tool call and returns nothing, so it must never
    # sit in the agent's tool loop.
    heartbeat_entry = settings["hooks"]["PostToolUse"][0]["hooks"][0]
    assert heartbeat_entry["async"] is True


def test_run_backs_up_pre_existing_settings_exactly_once(tmp_path, monkeypatch):
    """run() calls merge_hook seven times in one pass (the nudge hook plus six
    session-tracking hooks) — each one used to take its own backup, leaving six
    redundant copies of the same pre-init state after a single `ado init`. The
    timestamp suffix is only second-granular, so on fast, same-second test runs
    the seven calls happen to collide onto one filename and mask the bug — the
    fake clock below forces a distinct timestamp per call so the test actually
    proves it, regardless of execution speed."""
    from apo import installer

    checkout = tmp_path / "checkout"
    (checkout / "skills" / "decisions").mkdir(parents=True)
    (checkout / "hooks").mkdir(parents=True)
    (checkout / "hooks" / "session-decisions-nudge.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(installer, "__file__", str(checkout / "installer.py"))
    monkeypatch.setattr(installer, "register_mcp_server", lambda *a, **k: (True, "Registered."))

    real_datetime = installer.datetime
    ticks = iter(range(100))

    class FakeDatetime:
        @staticmethod
        def now():
            return real_datetime(2026, 1, 1, 0, 0, 0) + __import__("datetime").timedelta(seconds=next(ticks))

    monkeypatch.setattr(installer, "datetime", FakeDatetime)

    home = tmp_path / "home"
    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text('{"hooks": {}, "somePreExistingSetting": true}')

    args = argparse.Namespace(
        yes=True, projects_root=None, db_path=None, linear_api_key=None, linear_team=None,
        skip_linear=True, skip_mcp=True, skip_hook=False, skip_skill=True,
    )
    installer.run(args, home_dir=home)

    backups = list(settings_path.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["somePreExistingSetting"] is True


def test_run_respects_skip_flags(tmp_path, monkeypatch):
    from apo import installer

    checkout = tmp_path / "checkout"
    (checkout / "skills" / "decisions").mkdir(parents=True)
    (checkout / "hooks").mkdir(parents=True)
    (checkout / "hooks" / "session-decisions-nudge.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(installer, "__file__", str(checkout / "installer.py"))

    def fail_if_called(*a, **k):
        raise AssertionError("register_mcp_server should not be called when skipped")

    monkeypatch.setattr(installer, "register_mcp_server", fail_if_called)

    home = tmp_path / "home"
    args = argparse.Namespace(
        yes=True, projects_root=None, db_path=None, linear_api_key=None, linear_team=None,
        skip_linear=True, skip_mcp=True, skip_hook=True, skip_skill=True,
    )

    installer.run(args, home_dir=home)

    assert not (home / ".claude" / "skills" / "decisions").exists()
    assert not (home / ".claude" / "settings.json").exists()


def _init_args(**overrides):
    return argparse.Namespace(**{
        "yes": True, "db_path": None, "projects_root": None, "linear_api_key": None, "linear_team": None,
        "skip_linear": True, "skip_mcp": True, "skip_hook": True, "skip_skill": True, **overrides,
    })


def _env(home):
    path = home / ".claude" / "ai-decision-orchestrator" / ".env"
    return path.read_text() if path.exists() else ""


def test_init_asks_where_projects_live_and_saves_it(tmp_path, monkeypatch):
    from apo import installer

    prompts, answers = [], iter(["", str(tmp_path / "code")])
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or next(answers))
    home = tmp_path / "home"

    installer.run(_init_args(yes=False), home_dir=home)

    assert any(p.startswith("Where do your projects live?") for p in prompts)
    assert f"ADO_PROJECTS_ROOT={tmp_path / 'code'}" in _env(home)
    assert (tmp_path / "code").is_dir()


def test_pressing_enter_keeps_the_projects_folder_it_has(tmp_path, monkeypatch):
    from apo import installer
    from apo import config

    monkeypatch.setenv("ADO_PROJECTS_ROOT", str(tmp_path / "already"))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")
    home = tmp_path / "home"

    installer.run(_init_args(yes=False), home_dir=home)

    [asked] = [p for p in prompts if p.startswith("Where do your projects live?")]
    assert str(config.get_projects_root()) in asked
    assert "ADO_PROJECTS_ROOT" not in _env(home)


def test_the_projects_folder_can_come_from_a_flag(tmp_path, monkeypatch):
    from apo import installer

    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"prompted: {prompt}"))
    home = tmp_path / "home"

    installer.run(_init_args(projects_root=str(tmp_path / "work")), home_dir=home)

    assert f"ADO_PROJECTS_ROOT={tmp_path / 'work'}" in _env(home)


def test_yes_without_the_flag_leaves_the_projects_folder_alone(tmp_path):
    from apo import installer

    home = tmp_path / "home"

    installer.run(_init_args(), home_dir=home)

    assert "ADO_PROJECTS_ROOT" not in _env(home)
