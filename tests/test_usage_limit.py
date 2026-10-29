"""Hitting the usage limit isn't a fault. Claude Code continues an open session by
itself when the limit resets (its "Continue automatically at usage limit" setting),
so Now treats that as waiting, not a problem. Only a session that won't continue —
auto-continue off, or the terminal closed — becomes yours, with a way to resume it."""
import stat

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _limited(conn, tmp_path, sid="s-1"):
    db.register_session(conn, sid, str(tmp_path))
    db.session_prompt_submitted(conn, sid, "go")
    db.session_stop_failure(conn, sid, "rate_limit")


def test_waiting_for_the_reset_is_not_a_problem(db_path, tmp_path):
    conn = db.get_connection()
    _limited(conn, tmp_path)

    queue = db.now_queue(conn)

    assert queue["problems"] == [] and queue["your_move"] == []
    assert queue["quiet"]["usage_limit"] == 1


def test_the_event_says_usage_limit_not_api_error(db_path, tmp_path):
    conn = db.get_connection()
    _limited(conn, tmp_path)

    summaries = [e["summary"] for e in db.list_events(conn, min_importance=0)]

    assert any("usage limit" in s for s in summaries)
    assert not any("API error" in s for s in summaries)


def test_now_says_how_many_are_waiting_for_the_reset(client, tmp_path):
    _limited(db.get_connection(), tmp_path)

    assert "waiting for the usage limit" in client.get("/").text


def test_auto_continue_off_makes_it_yours(db_path, tmp_path):
    conn = db.get_connection()
    _limited(conn, tmp_path)

    db.session_quota_notification(conn, "s-1", "quota_auto_resume_disabled")

    problem = db.now_queue(conn)["problems"][0]
    assert "won't continue on its own" in problem["reason"]


def test_a_session_closed_while_limited_can_be_resumed_in_the_background(client, tmp_path, monkeypatch):
    conn = db.get_connection()
    _limited(conn, tmp_path)
    db.session_end(conn, "s-1", "prompt_input_exit")
    item = next(i for i in db.now_queue(conn)["your_move"] if i["kind"] == "limited")
    session_id = db.get_session(conn, "s-1")["id"]
    assert f'action="/sessions/{session_id}/resume"' in client.get("/").text

    log = tmp_path / "calls.log"
    fake = tmp_path / "fake-claude"
    fake.write_text(f'#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" >> {log}\necho "Started 3f2a9c1b"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("APO_CLAUDE_BIN", str(fake))

    resp = client.post(f"/sessions/{session_id}/resume", follow_redirects=False)

    assert resp.status_code == 303
    cwd, args = log.read_text().strip().split("|", 1)
    assert cwd == str(tmp_path)
    assert args.startswith("--bg --resume s-1 ") and "usage limit has reset" in args
    assert item["reason"].startswith("Closed while waiting for the usage limit")


def test_resume_refuses_a_session_that_is_still_open(client, tmp_path):
    conn = db.get_connection()
    _limited(conn, tmp_path)

    resp = client.post(f"/sessions/{db.get_session(conn, 's-1')['id']}/resume", follow_redirects=False)

    assert resp.status_code == 409
