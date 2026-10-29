"""Answering a background session's prompts from the dashboard: a permission request,
a question (AskUserQuestion) or a plan to approve (ExitPlanMode). The hook holds the
prompt and returns the dashboard's answer, which Claude Code applies as if it had
been given in its own dialog. Only background sessions are held: a terminal
session's own dialog must never wait on a web page."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def hooks(db_path, monkeypatch):
    from apo import cli

    # cli keeps the db module it was first imported with; db_path re-imports db.
    monkeypatch.setattr(cli.db, "PROMPT_HOLD_SECONDS", 3)
    monkeypatch.setattr(cli, "PROMPT_POLL_SECONDS", 0.02)
    return cli


@pytest.fixture
def folder(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    (tmp_path / "acme").mkdir()
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path / "acme"))
    return str(tmp_path / "acme")


def _session(sid, folder, kind="background"):
    conn = db.get_connection()
    db.register_session(conn, sid, folder)
    db.reconcile_live_sessions(conn, [{"id": "aa11bb22", "kind": kind, "sessionId": sid, "cwd": folder, "status": "busy"}])
    return conn.execute("SELECT id FROM sessions WHERE claude_session_id = ?", (sid,)).fetchone()["id"]


def _run(fn, payload):
    result = {}
    thread = threading.Thread(target=lambda: result.__setitem__("out", fn(payload)), daemon=True)
    thread.start()
    return thread, result


def _held(sid):
    for _ in range(200):
        prompt = db.get_session(db.get_connection(), sid)["pending_prompt"]
        if prompt:
            return json.loads(prompt)
        time.sleep(0.01)
    raise AssertionError("the hook never posted its prompt")


PERMISSION = {"session_id": "s-1", "tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}
QUESTION = {"session_id": "s-1", "tool_name": "AskUserQuestion", "tool_input": {"questions": [
    {"question": "Which framework?", "header": "Framework", "multiSelect": False,
     "options": [{"label": "React", "description": "Components"}, {"label": "Vue", "description": "Progressive"}]},
    {"question": "Which extras?", "header": "Extras", "multiSelect": True,
     "options": [{"label": "Router", "description": ""}, {"label": "Store", "description": ""}]},
]}}
PLAN = {"session_id": "s-1", "tool_name": "ExitPlanMode", "tool_input": {"plan": "1. Write the test\n2. Fix it"}}


def test_a_held_permission_is_allowed_from_the_dashboard(hooks, folder):
    _session("s-1", folder)
    thread, result = _run(hooks._permission, PERMISSION)

    prompt = _held("s-1")
    assert prompt["kind"] == "permission" and prompt["detail"] == "Bash: rm -rf build"
    assert db.answer_prompt(db.get_connection(), "s-1", prompt["id"], {"behavior": "allow"})
    thread.join(5)

    assert result["out"] == {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                    "decision": {"behavior": "allow"}}}
    assert db.get_session(db.get_connection(), "s-1")["pending_prompt"] is None


def test_two_permissions_at_once_are_asked_one_after_the_other(hooks, folder):
    _session("s-1", folder)
    first, first_result = _run(hooks._permission, PERMISSION)
    one = _held("s-1")
    second, second_result = _run(hooks._permission, {**PERMISSION, "tool_input": {"command": "ls"}})
    time.sleep(0.2)

    assert db.get_session(db.get_connection(), "s-1")["pending_prompt"] and _held("s-1")["id"] == one["id"]
    assert db.answer_prompt(db.get_connection(), "s-1", one["id"], {"behavior": "allow"})
    first.join(5)
    assert first_result["out"]["hookSpecificOutput"]["decision"] == {"behavior": "allow"}

    for _ in range(200):
        two = _held("s-1")
        if two["id"] != one["id"]:
            break
        time.sleep(0.01)
    assert two["detail"] == "Bash: ls"
    assert db.answer_prompt(db.get_connection(), "s-1", two["id"], {"behavior": "allow"})
    second.join(5)
    assert second_result["out"]["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


def test_a_denial_carries_its_reason_to_claude(hooks, folder):
    _session("s-1", folder)
    thread, result = _run(hooks._permission, PERMISSION)

    db.answer_prompt(db.get_connection(), "s-1", _held("s-1")["id"], {"behavior": "deny", "message": "Not the build dir"})
    thread.join(5)

    decision = result["out"]["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "deny", "message": "Not the build dir"}


def test_a_question_is_answered_with_the_chosen_options(hooks, folder):
    _session("s-1", folder)
    thread, result = _run(hooks._pretool, QUESTION)

    prompt = _held("s-1")
    assert prompt["kind"] == "question" and [q["question"] for q in prompt["questions"]] == ["Which framework?", "Which extras?"]
    db.answer_prompt(db.get_connection(), "s-1", prompt["id"],
                     {"answers": {"Which framework?": "React", "Which extras?": "Router, Store"}})
    thread.join(5)

    out = result["out"]["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "allow"
    assert out["updatedInput"] == {**QUESTION["tool_input"],
                                   "answers": {"Which framework?": "React", "Which extras?": "Router, Store"}}


def test_a_plan_is_approved_or_sent_back(hooks, folder):
    _session("s-1", folder)
    thread, result = _run(hooks._pretool, PLAN)
    prompt = _held("s-1")
    assert prompt["kind"] == "plan" and "Write the test" in prompt["plan"]
    db.answer_prompt(db.get_connection(), "s-1", prompt["id"], {"behavior": "allow"})
    thread.join(5)
    assert result["out"]["hookSpecificOutput"] == {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                   "updatedInput": PLAN["tool_input"]}

    thread, result = _run(hooks._pretool, PLAN)
    db.answer_prompt(db.get_connection(), "s-1", _held("s-1")["id"], {"behavior": "deny", "message": "Do step 2 first"})
    thread.join(5)
    assert result["out"]["hookSpecificOutput"] == {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                   "permissionDecisionReason": "Do step 2 first"}


def test_a_terminal_session_is_never_held(hooks, folder):
    _session("s-1", folder, kind="interactive")

    started = time.monotonic()
    assert hooks._permission(PERMISSION) is None
    assert time.monotonic() - started < 1
    assert db.get_session(db.get_connection(), "s-1")["pending_prompt"] is None


def test_an_unknown_session_is_looked_up_before_holding(hooks, folder, monkeypatch):
    from apo import claude_runtime

    db.register_session(db.get_connection(), "s-1", folder)
    monkeypatch.setattr(claude_runtime, "list_agents", lambda **kw: [
        {"id": "aa11bb22", "kind": "background", "sessionId": "s-1", "cwd": folder, "status": "waiting"}])
    thread, result = _run(hooks._permission, PERMISSION)

    db.answer_prompt(db.get_connection(), "s-1", _held("s-1")["id"], {"behavior": "allow"})
    thread.join(5)

    assert result["out"]["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


def test_an_unanswered_prompt_falls_back_to_claude_codes_own_dialog(hooks, folder, monkeypatch):
    monkeypatch.setattr(hooks.db, "PROMPT_HOLD_SECONDS", 0.2)
    _session("s-1", folder)

    assert hooks._permission(PERMISSION) is None
    assert db.get_session(db.get_connection(), "s-1")["pending_prompt"] is None


def test_answer_in_the_terminal_lets_the_dialog_show(hooks, folder):
    _session("s-1", folder)
    thread, result = _run(hooks._permission, PERMISSION)

    db.answer_prompt(db.get_connection(), "s-1", _held("s-1")["id"], {"behavior": "terminal"})
    thread.join(5)

    assert result["out"] is None


def test_a_stale_answer_is_refused(hooks, folder):
    _session("s-1", folder)
    thread, _ = _run(hooks._permission, PERMISSION)
    prompt = _held("s-1")

    assert not db.answer_prompt(db.get_connection(), "s-1", "not-the-prompt", {"behavior": "allow"})
    db.answer_prompt(db.get_connection(), "s-1", prompt["id"], {"behavior": "allow"})
    thread.join(5)
    assert not db.answer_prompt(db.get_connection(), "s-1", prompt["id"], {"behavior": "deny"})


# --- the dashboard ---

def test_the_card_offers_allow_and_deny_for_a_held_permission(client, hooks, folder):
    sid = _session("s-1", folder)
    thread, result = _run(hooks._permission, PERMISSION)
    prompt = _held("s-1")
    project_id = db.get_session(db.get_connection(), "s-1")["project_id"]

    html = client.get(f"/projects/{project_id}/agents").text
    assert f'action="/sessions/{sid}/prompt"' in html and "Bash: rm -rf build" in html
    resp = client.post(f"/sessions/{sid}/prompt", data={"prompt_id": prompt["id"], "choice": "allow"},
                       follow_redirects=False)
    thread.join(5)

    assert resp.status_code == 303
    assert result["out"]["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


def test_the_session_page_answers_questions(client, hooks, folder):
    sid = _session("s-1", folder)
    thread, result = _run(hooks._pretool, QUESTION)
    prompt = _held("s-1")

    html = client.get(f"/sessions/{sid}").text
    for text in ("Which framework?", "React", "Vue", "Which extras?", "Router", "Store"):
        assert text in html, text
    client.post(f"/sessions/{sid}/prompt", data={"prompt_id": prompt["id"], "choice": "answer",
                                                  "answer-0": "Vue", "answer-1": ["Router", "Store"]})
    thread.join(5)

    assert result["out"]["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Which framework?": "Vue", "Which extras?": "Router, Store"}


def test_a_question_can_take_your_own_words(client, hooks, folder):
    sid = _session("s-1", folder)
    thread, result = _run(hooks._pretool, QUESTION)
    prompt = _held("s-1")

    client.post(f"/sessions/{sid}/prompt", data={"prompt_id": prompt["id"], "choice": "answer",
                                                  "other-0": "Svelte", "answer-1": "Router"})
    thread.join(5)

    assert result["out"]["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Which framework?": "Svelte", "Which extras?": "Router"}


def test_the_session_page_approves_a_plan(client, hooks, folder):
    sid = _session("s-1", folder)
    thread, result = _run(hooks._pretool, PLAN)
    prompt = _held("s-1")

    assert "Write the test" in client.get(f"/sessions/{sid}").text
    client.post(f"/sessions/{sid}/prompt", data={"prompt_id": prompt["id"], "choice": "deny", "message": "Do step 2 first"})
    thread.join(5)

    assert result["out"]["hookSpecificOutput"]["permissionDecisionReason"] == "Do step 2 first"


def test_a_stale_dashboard_answer_says_so(client, hooks, folder):
    sid = _session("s-1", folder)

    resp = client.post(f"/sessions/{sid}/prompt", data={"prompt_id": "gone", "choice": "allow"}, follow_redirects=False)

    assert "error=" in resp.headers["location"]


def test_ado_init_makes_the_prompt_hooks_wait_for_an_answer():
    from apo import installer

    hooks = {event: opts for event, _, opts in installer.SESSION_HOOKS if event in ("PermissionRequest", "PreToolUse")}
    for opts in hooks.values():
        assert not opts.get("run_async") and opts["timeout"] > db.PROMPT_HOLD_SECONDS
