"""Whether an answer reaches an agent. Live: four answers sat for up to 12.7
days with nothing to pick them up and nothing saying so. Answered is in transit
until an agent applies it; the session that asked hears about it on its next
prompt; and an answer nothing can pick up comes back to the human as a move."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apo import db



def _filed(conn, tmp_path, session="sess-1", **fields):
    project = db.get_or_create_project(conn, "acme")
    repo = db.get_repository(conn, db.ensure_repository_for_project(conn, project["id"], str(tmp_path)))
    db.register_session(conn, session, str(tmp_path))
    return db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], origin_session_id=session,
        **{"title": "Queue backend", "question": "Postgres or SQLite?", **fields},
    )


def test_delivery_moves_from_pending_to_applied(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _filed(conn, tmp_path)
    assert db.get_decision_request(conn, decision_id)["delivery"] == "none"

    db.answer_decision_request(conn, decision_id, "Postgres")
    assert db.get_decision_request(conn, decision_id)["delivery"] == "pending"

    db.resolve_decision_request(conn, decision_id)
    assert db.get_decision_request(conn, decision_id)["delivery"] == "applied"


def test_the_session_that_asked_hears_the_answer_once(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _filed(conn, tmp_path)
    db.answer_decision_request(conn, decision_id, "Postgres")

    first = db.take_answers_for_session(conn, "sess-1")
    second = db.take_answers_for_session(conn, "sess-1")

    assert [d["id"] for d in first] == [decision_id]
    assert second == []
    row = db.get_decision_request(conn, decision_id)
    assert row["delivery"] == "delivered"
    assert row["delivered_to_session"] == "sess-1"


def test_other_sessions_are_not_handed_someone_elses_answer(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _filed(conn, tmp_path)
    db.register_session(conn, "other", str(tmp_path))
    db.answer_decision_request(conn, decision_id, "Postgres")

    assert db.take_answers_for_session(conn, "other") == []


def _run_prompt_hook(db_path, prompt="go on"):
    env = dict(os.environ, ADO_DB=str(db_path))
    return subprocess.run(
        [sys.executable, "-m", "apo", "session-prompt"], capture_output=True, text=True, env=env,
        input=json.dumps({"session_id": "sess-1", "hook_event_name": "UserPromptSubmit", "prompt": prompt}),
    )


def test_prompt_hook_adds_the_answer_to_the_asking_sessions_context(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _filed(conn, tmp_path, title="Queue backend")
    db.answer_decision_request(conn, decision_id, "Chose option 1: Postgres")

    result = _run_prompt_hook(db_path)

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    context = output["additionalContext"]
    assert f"#{decision_id}" in context and "Queue backend" in context and "Chose option 1: Postgres" in context
    assert f"mark_decision_resolved({decision_id})" in context
    assert json.loads(_run_prompt_hook(db_path).stdout) == {}


def test_prompt_hook_prints_empty_json_when_nothing_is_waiting(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    assert json.loads(_run_prompt_hook(db_path).stdout) == {}


def test_in_transit_lists_answers_not_yet_applied(db_path, tmp_path):
    conn = db.get_connection()
    waiting = _filed(conn, tmp_path, title="Waiting")
    applied = _filed(conn, tmp_path, title="Applied")
    db.answer_decision_request(conn, waiting, "yes")
    db.answer_decision_request(conn, applied, "yes")
    db.resolve_decision_request(conn, applied)

    assert [d["id"] for d in db.list_in_transit(conn)] == [waiting]


def test_an_old_answer_with_no_live_session_in_its_repo_is_stuck(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _filed(conn, tmp_path)
    db.answer_decision_request(conn, decision_id, "yes")
    db.session_end(conn, "sess-1", "logout")
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    conn.execute("UPDATE decision_requests SET answered_at = ?", (old,))
    conn.commit()

    item = db.list_in_transit(conn)[0]

    assert item["stuck"] is True
    assert item["resume_command"] == f'cd {tmp_path} && claude --resume sess-1 "/decisions"'


def test_a_fresh_answer_or_one_with_a_live_session_is_not_stuck(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _filed(conn, tmp_path)
    db.answer_decision_request(conn, decision_id, "yes")
    assert db.list_in_transit(conn)[0]["stuck"] is False

    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    conn.execute("UPDATE decision_requests SET answered_at = ?", (old,))
    conn.commit()
    item = db.list_in_transit(conn)[0]
    assert item["stuck"] is False  # sess-1 is still live in that repo
    assert item["live_session"] == "sess-1"


def _page(db_path, decision_id):
    from fastapi.testclient import TestClient

    from apo.web import app

    return TestClient(app.app).get(f"/decisions/{decision_id}").text


def test_decision_page_offers_a_resume_command_when_nothing_will_pick_it_up(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _filed(conn, tmp_path)
    db.answer_decision_request(conn, decision_id, "yes")
    db.session_end(conn, "sess-1", "logout")
    conn.execute("UPDATE decision_requests SET answered_at = '2026-01-01T00:00:00+00:00'")
    conn.commit()

    text = _page(db_path, decision_id)

    assert "Nothing is running" in text
    assert f"claude --resume sess-1 &#34;/decisions&#34;" in text or 'claude --resume sess-1 "/decisions"' in text
    assert "data-copy" in text


def test_decision_page_says_when_the_answer_was_delivered(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _filed(conn, tmp_path)
    db.answer_decision_request(conn, decision_id, "yes")
    db.take_answers_for_session(conn, "sess-1")

    assert "Delivered to the session that asked" in _page(db_path, decision_id)


def test_upgrading_marks_existing_answers_pending_and_resolved_ones_applied(db_path, tmp_path):
    conn = db.get_connection()
    answered = _filed(conn, tmp_path, title="A")
    resolved = _filed(conn, tmp_path, title="R")
    db.answer_decision_request(conn, answered, "x")
    db.answer_decision_request(conn, resolved, "y")
    db.resolve_decision_request(conn, resolved)
    conn.execute("UPDATE decision_requests SET delivery = NULL")
    conn.execute("PRAGMA user_version = 8")
    conn.commit()

    conn = db.get_connection()

    assert db.get_decision_request(conn, answered)["delivery"] == "pending"
    assert db.get_decision_request(conn, resolved)["delivery"] == "applied"
