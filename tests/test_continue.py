"""Replying to a session from the dashboard. Continuing it resumes the conversation,
so the next turn re-reads everything it carries; continuing fresh starts a new
session from a short handoff instead, tracked as a task like any other dashboard
start. The page recommends fresh once a session is big or its cache has gone cold."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def calls(client, monkeypatch):
    from apo import claude_runtime

    log = []
    monkeypatch.setattr(claude_runtime, "start_background",
                        lambda path, prompt, resume=None, **kw: log.append(("start", path, prompt, resume)) or "9c8b7a6d")
    monkeypatch.setattr(claude_runtime, "stop_background", lambda agent_id, **kw: log.append(("stop", agent_id)) or True)
    return log


@pytest.fixture
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    (tmp_path / "acme").mkdir()
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path / "acme"))
    return project, str(tmp_path / "acme")


def _session(conn, sid, folder, kind="background", ended=False, context=20_000, idle_minutes=1):
    db.register_session(conn, sid, folder)
    db.reconcile_live_sessions(conn, [{"id": "aa11bb22", "kind": kind, "sessionId": sid, "cwd": folder, "status": "idle"}])
    db.session_stop(conn, sid)
    db.record_session_summary(conn, sid, "Fixed the login test.", last_prompt="Fix the flaky login test",
                              context_tokens=context)
    stopped = (datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)).isoformat()
    conn.execute("UPDATE sessions SET last_stop_at = ?, last_seen_at = ? WHERE claude_session_id = ?",
                 (stopped, stopped, sid))
    conn.commit()
    if ended:
        db.session_end(conn, sid, "exit")
    return conn.execute("SELECT id FROM sessions WHERE claude_session_id = ?", (sid,)).fetchone()["id"]


def test_continuing_an_idle_background_session_stops_it_then_resumes_it_with_the_message(client, acme, calls):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder)

    resp = client.post(f"/sessions/{sid}/continue", data={"message": "Now add a test for logout", "how": "resume"},
                       follow_redirects=False)

    assert resp.status_code == 303
    assert calls == [("stop", "aa11bb22"), ("start", folder, "Now add a test for logout", "s-1")]


def test_continuing_an_ended_session_just_resumes_it(client, acme, calls):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder, ended=True)

    client.post(f"/sessions/{sid}/continue", data={"message": "Now add a test for logout", "how": "resume"})

    assert calls == [("start", folder, "Now add a test for logout", "s-1")]


def test_continuing_fresh_starts_a_tracked_task_from_a_handoff(client, acme, calls):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder)

    client.post(f"/sessions/{sid}/continue", data={"message": "Now add a test for logout", "how": "fresh"})

    conn = db.get_connection()
    [task] = db.list_tasks(conn, project["id"])
    item = db.get_decision_request(conn, task["id"])
    assert item["title"] == "Now add a test for logout"
    for text in ("Now add a test for logout", "Fix the flaky login test", "Fixed the login test.", "s-1"):
        assert text in item["question"], text
    assert calls == [("start", folder, f"/decisions #{task['id']} --here", None)]


def test_a_terminal_session_still_open_is_answered_in_its_terminal(client, acme, calls):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder, kind="interactive")

    html = client.get(f"/sessions/{sid}").text
    resp = client.post(f"/sessions/{sid}/continue", data={"message": "More", "how": "resume"}, follow_redirects=False)

    assert f'action="/sessions/{sid}/continue"' not in html and "terminal" in html
    assert "error=" in resp.headers["location"] and calls == []


def test_a_session_mid_turn_cant_be_continued(client, acme, calls):
    project, folder = acme
    conn = db.get_connection()
    sid = _session(conn, "s-1", folder)
    db.session_heartbeat(conn, "s-1", folder)

    resp = client.post(f"/sessions/{sid}/continue", data={"message": "More", "how": "resume"}, follow_redirects=False)

    assert "error=" in resp.headers["location"] and calls == []


def test_a_message_is_needed(client, acme, calls):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder)

    resp = client.post(f"/sessions/{sid}/continue", data={"message": "  ", "how": "fresh"}, follow_redirects=False)

    assert "error=" in resp.headers["location"] and calls == []


def _recommended(html):
    form = re.search(r'<form[^>]*action="/sessions/\d+/continue".*?</form>', html, re.S).group(0)
    return re.search(r'<button[^>]*btn--primary[^>]*value="(\w+)"', form).group(1)


def test_the_page_recommends_continuing_a_small_recent_session(client, acme):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder, context=20_000, idle_minutes=1)

    assert _recommended(client.get(f"/sessions/{sid}").text) == "resume"


def test_the_page_recommends_fresh_for_a_big_session(client, acme):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder, context=180_000, idle_minutes=1)

    html = client.get(f"/sessions/{sid}").text
    assert _recommended(html) == "fresh" and "180k" in html


def test_the_page_recommends_fresh_once_the_cache_has_gone_cold(client, acme):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder, context=20_000, idle_minutes=45)

    assert _recommended(client.get(f"/sessions/{sid}").text) == "fresh"


def test_resuming_an_ended_session_needs_no_message_and_returns_where_you_were(client, acme, calls):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder, ended=True)
    agents = f"/projects/{project['id']}/agents"

    resp = client.post(f"/sessions/{sid}/continue", data={"how": "resume", "next": agents}, follow_redirects=False)

    assert resp.headers["location"] == agents
    assert calls == [("start", folder, db.RESUME_PROMPT, "s-1")]


def test_a_failed_resume_says_so_where_you_were(client, acme, monkeypatch):
    from apo import claude_runtime

    def fail(*args, **kwargs):
        raise claude_runtime.LaunchError("Workspace not trusted.")

    monkeypatch.setattr(claude_runtime, "start_background", fail)
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder, ended=True)
    agents = f"/projects/{project['id']}/agents"

    resp = client.post(f"/sessions/{sid}/continue", data={"how": "resume", "next": agents}, follow_redirects=False)

    assert resp.headers["location"].startswith(f"{agents}?error=")
    assert "Workspace not trusted." in client.get(resp.headers["location"]).text


def _resume_form(html, sid):
    match = re.search(rf'<form[^>]*action="/sessions/{sid}/continue".*?</form>', html, re.S)
    return match and match.group(0)


def test_the_projects_agents_tab_can_resume_what_ended_recently(client, acme):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder, ended=True)

    html = client.get(f"/projects/{project['id']}/agents").text
    ended = html[html.index("Ended recently"):]

    assert f'href="/sessions/{sid}"' in ended
    form = _resume_form(ended, sid)
    assert 'value="resume"' in form and f'name="next" value="/projects/{project["id"]}/agents"' in form
    assert "claude --resume s-1" in ended


def test_the_all_projects_agents_page_can_resume_what_ended_recently(client, acme):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder, ended=True)

    html = client.get("/projects/agents").text

    assert 'name="next" value="/projects/agents"' in _resume_form(html, sid)
    assert "claude --resume s-1" in html


def test_a_session_still_open_in_a_terminal_has_no_resume(client, acme):
    project, folder = acme
    sid = _session(db.get_connection(), "s-1", folder, kind="interactive")

    assert _resume_form(client.get(f"/projects/{project['id']}/agents").text, sid) is None
    assert _resume_form(client.get("/projects/agents").text, sid) is None
