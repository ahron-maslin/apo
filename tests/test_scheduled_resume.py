"""Resume a session when the usage limit resets — for one that was closed while it
waited (an open one, Claude Code continues by itself). The reset time comes from
Claude Code's own message ("resets 4pm (Europe/Berlin)") when it says one."""
import stat
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from apo import db

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.mark.parametrize("text,expected", [
    ("You've hit your limit · resets 4pm (Europe/Berlin)", datetime(2026, 10, 15, 16, 0, tzinfo=BERLIN)),
    ("Usage limit reached · resets 4:30pm (Europe/Berlin)", datetime(2026, 10, 15, 16, 30, tzinfo=BERLIN)),
    ("limit reached, resets at 9am (Europe/Berlin)", datetime(2026, 10, 16, 9, 0, tzinfo=BERLIN)),
    ("spend limit reached (daily; resets 2026-10-16 00:00 UTC)", datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)),
])
def test_the_reset_time_is_read_from_claude_codes_message(text, expected):
    now = datetime(2026, 10, 15, 10, 0, tzinfo=BERLIN)

    assert db.parse_reset_time(text, now) == expected


def test_no_reset_time_in_the_message_means_none():
    assert db.parse_reset_time("API Error: overloaded", datetime.now(timezone.utc)) is None


def _closed_while_limited(conn, tmp_path, message="You've hit your limit · resets 4pm (Europe/Berlin)"):
    db.register_session(conn, "s-1", str(tmp_path))
    db.session_prompt_submitted(conn, "s-1", "go")
    db.session_stop_failure(conn, "s-1", "rate_limit", message=message)
    db.session_end(conn, "s-1", "prompt_input_exit")
    return db.get_session(conn, "s-1")


def test_the_stop_records_when_the_limit_resets(db_path, tmp_path):
    session = _closed_while_limited(db.get_connection(), tmp_path)

    assert session["limit_resets_at"]
    assert "resets 4pm" in session["last_error_detail"]


def test_scheduling_moves_it_out_of_your_move(client, tmp_path):
    conn = db.get_connection()
    session = _closed_while_limited(conn, tmp_path)
    assert 'name="at"' in client.get("/").text

    client.post(f"/sessions/{session['id']}/resume", data={"at": "16:00"})

    assert db.get_session(conn, "s-1")["resume_scheduled_at"]
    queue = db.now_queue(conn)
    assert not [i for i in queue["your_move"] if i["kind"] == "limited"]
    waiting = [i for i in queue["in_transit"] if i["kind"] == "limited"]
    assert waiting and waiting[0]["reason"].startswith("Resumes at 16:00")


def _fake_claude(tmp_path, monkeypatch, fail=False):
    log = tmp_path / "calls.log"
    fake = tmp_path / "fake-claude"
    body = "echo 'still limited' >&2; exit 1" if fail else 'echo "Started 3f2a9c1b"'
    fake.write_text(f'#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" >> {log}\n{body}\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ADO_CLAUDE_BIN", str(fake))
    return log


def test_a_due_resume_starts_the_session_once(db_path, tmp_path, monkeypatch):
    from apo.web import app

    conn = db.get_connection()
    _closed_while_limited(conn, tmp_path)
    db.schedule_resume(conn, "s-1", datetime.now(timezone.utc) - timedelta(minutes=1))
    log = _fake_claude(tmp_path, monkeypatch)

    app.run_scheduled_resumes()
    app.run_scheduled_resumes()

    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1 and lines[0].split("|")[1].startswith("--bg --resume s-1 ")
    assert db.get_session(conn, "s-1")["resume_scheduled_at"] is None


def test_a_failed_resume_retries_then_gives_up(db_path, tmp_path, monkeypatch):
    from apo.web import app

    conn = db.get_connection()
    _closed_while_limited(conn, tmp_path)
    log = _fake_claude(tmp_path, monkeypatch, fail=True)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db.schedule_resume(conn, "s-1", datetime.now(timezone.utc) - timedelta(minutes=1))

    for _ in range(db.RESUME_ATTEMPTS + 1):
        # Each retry is due 15 minutes later; pretend that time has passed.
        conn.execute("UPDATE sessions SET resume_scheduled_at = ? WHERE resume_scheduled_at IS NOT NULL", (past,))
        conn.commit()
        app.run_scheduled_resumes()

    assert len(log.read_text().strip().splitlines()) == db.RESUME_ATTEMPTS
    assert db.get_session(conn, "s-1")["resume_scheduled_at"] is None
    assert any("Couldn't resume" in e["summary"] for e in db.list_events(conn, min_importance=0))


def test_the_dashboard_lifespan_is_a_context_manager_and_resumes_run_directly(db_path):
    from apo.web import app

    assert hasattr(app.lifespan(app.app), "__aenter__")
    assert app.run_scheduled_resumes() is None
