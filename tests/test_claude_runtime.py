"""Zero-token view of the live Claude Code processes on this machine.

A stdio MCP server outlives /clear and /resume, so the CLAUDE_CODE_SESSION_ID in its
environment goes stale; the session record Claude Code keeps per process
(~/.claude/sessions/<pid>.json) does not.
"""
import json
import os
import stat
from pathlib import Path

from apo import claude_runtime


def _write_record(pid: int, **fields) -> Path:
    sessions = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"{pid}.json"
    path.write_text(json.dumps({"pid": pid, **fields}))
    return path


def test_session_for_pid_reads_a_live_process_record():
    me = os.getpid()
    _write_record(me, sessionId="live-1", procStart=claude_runtime.process_start_ticks(me), status="busy")

    record = claude_runtime.session_for_pid(me)

    assert record["sessionId"] == "live-1"
    assert record["status"] == "busy"


def test_session_for_pid_ignores_a_record_left_by_a_reused_pid():
    me = os.getpid()
    _write_record(me, sessionId="old-1", procStart="1")

    assert claude_runtime.session_for_pid(me) is None


def test_session_for_pid_tolerates_a_missing_or_garbled_record():
    assert claude_runtime.session_for_pid(os.getpid()) is None
    _write_record(os.getpid())
    (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "sessions" / f"{os.getpid()}.json").write_text("{not json")
    assert claude_runtime.session_for_pid(os.getpid()) is None


def test_current_session_id_prefers_the_process_record(monkeypatch):
    me = os.getpid()
    _write_record(me, sessionId="live-1", procStart=claude_runtime.process_start_ticks(me))
    monkeypatch.setattr(claude_runtime, "list_agents", lambda **k: [{"pid": me, "sessionId": "other"}])

    assert claude_runtime.current_session_id(me) == "live-1"


def test_current_session_id_falls_back_to_claude_agents(monkeypatch):
    monkeypatch.setattr(
        claude_runtime, "list_agents",
        lambda **k: [{"pid": 1, "sessionId": "nope"}, {"pid": 4242, "sessionId": "from-agents"}],
    )
    assert claude_runtime.current_session_id(4242) == "from-agents"


def test_current_session_id_is_none_when_nothing_knows_the_pid():
    assert claude_runtime.current_session_id(4242) is None


def test_list_agents_is_none_when_the_claude_binary_is_missing():
    assert claude_runtime.list_agents() is None


def test_list_agents_parses_the_json_list(tmp_path, monkeypatch):
    fake = tmp_path / "fake-claude"
    fake.write_text('#!/bin/sh\necho \'[{"pid": 7, "sessionId": "s", "status": "idle"}]\'\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("APO_CLAUDE_BIN", str(fake))

    assert claude_runtime.list_agents() == [{"pid": 7, "sessionId": "s", "status": "idle"}]


def _fake_claude(tmp_path, monkeypatch, script):
    fake = tmp_path / "fake-claude"
    fake.write_text("#!/bin/sh\n" + script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("APO_CLAUDE_BIN", str(fake))


def test_other_claude_profiles_are_named_in_ado_claude_config_dirs(tmp_path, monkeypatch):
    current = Path(os.environ["CLAUDE_CONFIG_DIR"])
    monkeypatch.setenv("APO_CLAUDE_CONFIG_DIRS", os.pathsep.join([str(tmp_path / "work"), "", str(current), str(tmp_path / "work")]))

    assert claude_runtime.claude_config_dirs() == [current, tmp_path / "work"]


def test_list_agents_merges_every_profile(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setenv("APO_CLAUDE_CONFIG_DIRS", str(work))
    _fake_claude(tmp_path, monkeypatch,
                 f'if [ "$CLAUDE_CONFIG_DIR" = "{work}" ]; then echo \'[{{"pid": 8, "sessionId": "w"}}]\'; '
                 'else echo \'[{"pid": 7, "sessionId": "s"}]\'; fi\n')

    assert claude_runtime.list_agents() == [{"pid": 7, "sessionId": "s"}, {"pid": 8, "sessionId": "w"}]


def test_list_agents_is_none_when_any_profile_cannot_be_read(tmp_path, monkeypatch):
    """Half a picture would end every live session of the profile that failed."""
    work = tmp_path / "work"
    monkeypatch.setenv("APO_CLAUDE_CONFIG_DIRS", str(work))
    _fake_claude(tmp_path, monkeypatch,
                 f'if [ "$CLAUDE_CONFIG_DIR" = "{work}" ]; then exit 1; fi\necho \'[{{"pid": 7, "sessionId": "s"}}]\'\n')

    assert claude_runtime.list_agents() is None


def test_the_current_profile_is_listed_with_the_environment_untouched(tmp_path, monkeypatch):
    """Setting CLAUDE_CONFIG_DIR to the default ~/.claude is not the same as leaving
    it unset: Claude Code then reads ~/.claude/.claude.json instead of ~/.claude.json."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    _fake_claude(tmp_path, monkeypatch, 'echo "[{\\"pid\\": 1, \\"sessionId\\": \\"${CLAUDE_CONFIG_DIR:-unset}\\"}]"\n')

    assert claude_runtime.list_agents() == [{"pid": 1, "sessionId": "unset"}]


def test_session_for_pid_finds_a_record_kept_by_another_profile(tmp_path, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setenv("APO_CLAUDE_CONFIG_DIRS", str(work))
    me = os.getpid()
    (work / "sessions").mkdir(parents=True)
    (work / "sessions" / f"{me}.json").write_text(json.dumps({"pid": me, "sessionId": "globex-1", "procStart": claude_runtime.process_start_ticks(me)}))

    assert claude_runtime.session_for_pid(me)["sessionId"] == "globex-1"
