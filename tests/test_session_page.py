"""A session's own page: the whole conversation from its transcript (what you asked,
what it said, the tools it ran, Claude Code's recaps), how much context its next
turn re-reads, and a recap you can ask for. The recap comes from a small model
through Claude Code's own sign-in, with hooks, tools and MCP off, so it never shows
up as a session itself."""
import html as html_lib
import json
import re

import pytest
from fastapi.testclient import TestClient

from apo import db
from apo import transcripts


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _line(**entry):
    return json.dumps(entry)


TRANSCRIPT = "\n".join([
    _line(type="ai-title", aiTitle="Flaky login test"),
    _line(type="user", origin={"kind": "human"}, timestamp="2026-10-21T10:00:00Z",
          message={"role": "user", "content": "Fix the flaky login test\nIt fails one run in five."}),
    _line(type="assistant", timestamp="2026-10-21T10:00:05Z", message={
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": "Looking at the test first."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest tests/test_login.py"}}],
        "usage": {"input_tokens": 3, "cache_read_input_tokens": 120000, "cache_creation_input_tokens": 2000,
                  "output_tokens": 50}}),
    _line(type="user", timestamp="2026-10-21T10:00:09Z",
          message={"role": "user", "content": [{"type": "tool_result", "content": "1 failed"}]}),
    _line(type="assistant", isSidechain=True, timestamp="2026-10-21T10:00:10Z",
          message={"content": [{"type": "text", "text": "A subagent's own chatter"}]}),
    _line(type="assistant", timestamp="2026-10-21T10:01:00Z", message={
        "model": "claude-opus-5-5", "content": [{"type": "text", "text": "Fixed: the test now waits for the redirect."}],
        "usage": {"input_tokens": 5, "cache_read_input_tokens": 150000, "cache_creation_input_tokens": 1000,
                  "output_tokens": 80}}),
    _line(type="system", subtype="away_summary", timestamp="2026-10-21T10:30:00Z",
          content="Fixed the flaky login test by waiting for the redirect."),
])


@pytest.fixture
def session(db_path, tmp_path, monkeypatch):
    config = tmp_path / "claude-config"
    path = config / "projects" / "-home-acme" / "s-1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(TRANSCRIPT + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    (tmp_path / "acme").mkdir()
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path / "acme"))
    db.register_session(conn, "s-1", str(tmp_path / "acme"))
    row = conn.execute("SELECT id FROM sessions WHERE claude_session_id = 's-1'").fetchone()
    return {"id": row["id"], "path": path, "project": project}


def test_the_conversation_is_the_main_thread_oldest_first(session):
    convo = transcripts.conversation(str(session["path"]))

    assert [(e["kind"], e["text"]) for e in convo["entries"]] == [
        ("prompt", "Fix the flaky login test\nIt fails one run in five."),
        ("assistant", "Looking at the test first."),
        ("tool", "Bash: pytest tests/test_login.py"),
        ("assistant", "Fixed: the test now waits for the redirect."),
        ("recap", "Fixed the flaky login test by waiting for the redirect."),
    ]
    assert convo["context_tokens"] == 151005 and convo["model"] == "claude-opus-5-5"


def test_a_sessions_transcript_is_found_by_its_id(session):
    from apo import claude_runtime

    assert transcripts.find("s-1", claude_runtime.claude_config_dirs()) == session["path"]
    assert transcripts.find("nope", claude_runtime.claude_config_dirs()) is None


def test_the_stop_hook_records_how_much_context_the_session_carries(session):
    info = transcripts.tail_info(str(session["path"]))
    db.record_session_summary(db.get_connection(), "s-1", "Done", context_tokens=info["context_tokens"])

    assert info["context_tokens"] == 151005
    assert db.get_session(db.get_connection(), "s-1")["context_tokens"] == 151005


def test_the_session_page_shows_the_whole_conversation(client, session):
    html = client.get(f"/sessions/{session['id']}").text

    for text in ("Fix the flaky login test", "Looking at the test first.", "Bash: pytest tests/test_login.py",
                 "Fixed: the test now waits for the redirect.", "151k"):
        assert text in html, text
    assert "A subagent&#39;s own chatter" not in html and "1 failed" not in html
    assert f'action="/sessions/{session["id"]}/recap"' in html


def test_a_session_without_a_transcript_still_has_a_page(client, session):
    session["path"].unlink()

    resp = client.get(f"/sessions/{session['id']}")

    assert resp.status_code == 200 and "transcript" in resp.text.lower()


def test_an_unknown_session_is_not_found(client, db_path):
    assert client.get("/sessions/999").status_code == 404


def test_cards_show_context_size_and_link_to_the_page(client, session):
    db.record_session_summary(db.get_connection(), "s-1", "Done", context_tokens=151005)

    html = client.get(f"/projects/{session['project']['id']}/agents").text

    assert f'href="/sessions/{session["id"]}"' in html and "151k" in html


def test_generating_a_recap_summarizes_the_conversation(client, session, monkeypatch):
    from apo import claude_runtime

    seen = []
    monkeypatch.setattr(claude_runtime, "summarize", lambda text: seen.append(text) or "It fixed the login test.")

    resp = client.post(f"/sessions/{session['id']}/recap", follow_redirects=False)

    assert resp.status_code == 303 and resp.headers["location"] == f"/sessions/{session['id']}"
    assert "Fix the flaky login test" in seen[0] and "pytest tests/test_login.py" in seen[0]
    assert db.get_session(db.get_connection(), "s-1")["last_summary"] == "It fixed the login test."


def test_a_failed_recap_says_why(client, session, monkeypatch):
    from apo import claude_runtime

    def fail(text):
        raise claude_runtime.LaunchError("claude is not signed in")

    monkeypatch.setattr(claude_runtime, "summarize", fail)

    resp = client.post(f"/sessions/{session['id']}/recap", follow_redirects=False)

    assert "error=" in resp.headers["location"]


def test_summarize_runs_claude_without_hooks_tools_mcp_or_a_saved_session(db_path, monkeypatch):
    from apo import claude_runtime

    calls = []

    class Done:
        returncode, stdout, stderr = 0, "A recap.\n", ""

    monkeypatch.setattr(claude_runtime.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or Done())

    assert claude_runtime.summarize("the conversation") == "A recap."
    cmd, kw = calls[0]
    for flag in ("-p", "--no-session-persistence", "--strict-mcp-config"):
        assert flag in cmd, flag
    assert cmd[cmd.index("--model") + 1] == "haiku" and cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True}
    assert kw["input"] == "the conversation"


def test_the_session_page_shows_the_whole_terminal_command_with_a_copy_button(client, session, tmp_path):
    html = client.get(f"/sessions/{session['id']}").text
    command = html_lib.escape(f"cd {tmp_path / 'acme'} && claude --resume s-1")

    assert f'<code class="terminal-command">{command}</code>' in html and f'data-copy="{command}"' in html


def test_a_session_without_a_folder_gets_the_bare_resume_command(client, db_path):
    conn = db.get_connection()
    db.register_session(conn, "s-2", None)
    sid = conn.execute("SELECT id FROM sessions WHERE claude_session_id = 's-2'").fetchone()["id"]

    html = client.get(f"/sessions/{sid}").text

    assert 'data-copy="claude --resume s-2"' in html


def _tool_call(at, *commands):
    return _line(type="assistant", timestamp=at, message={"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": c}} for c in commands]})


def _tool_groups(page):
    """Each folded run of tool calls on the page: (its id, how many calls)."""
    return [(gid, int(n)) for gid, n in re.findall(
        r'<details class="disclosure" id="([^"]+)">\s*<summary>.*?(\d+) tool calls?</summary>', page, re.S)]


def test_a_run_of_tool_calls_keeps_its_id_while_more_calls_join_it(client, session):
    """The live refresh reopens a group you expanded by its id, so the id can't move as the group grows."""
    page = f"/sessions/{session['id']}"
    session["path"].write_text(TRANSCRIPT + "\n" + _tool_call("2026-10-21T10:31:00Z", "git status") + "\n")
    (first, _), (growing, one) = _tool_groups(client.get(page).text)

    with session["path"].open("a") as f:
        f.write(_tool_call("2026-10-21T10:31:05Z", "git diff") + "\n")

    assert one == 1 and _tool_groups(client.get(page).text) == [(first, 1), (growing, 2)]
    assert first != growing


def test_tool_runs_started_by_one_message_get_ids_of_their_own(client, session):
    session["path"].write_text(_line(type="assistant", timestamp="2026-10-21T10:00:05Z", message={"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
        {"type": "text", "text": "Now the other file."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "b.py"}}]}) + "\n")

    ids = [gid for gid, _ in _tool_groups(client.get(f"/sessions/{session['id']}").text)]

    assert len(ids) == 2 and len(set(ids)) == 2
