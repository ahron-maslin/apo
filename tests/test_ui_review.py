"""Fixes from the UI review against real data: repeats collapse, "unknown" isn't a
reason, recaps aren't raw markdown tables, and you see a recommendation before you
accept it."""
import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return project


def _errored_three_times(conn, tmp_path):
    db.register_session(conn, "s-1", str(tmp_path))
    for _ in range(3):
        db.session_prompt_submitted(conn, "s-1", "go")
        db.session_stop_failure(conn, "s-1", "unknown")


def test_repeated_events_collapse_into_one_with_a_count(acme, tmp_path):
    conn = db.get_connection()
    _errored_three_times(conn, tmp_path)

    events = db.collapse_repeats(db.list_events(conn, project_id=acme["id"]))

    errored = [e for e in events if e["verb"] == "session.errored"]
    assert len(errored) == 1 and errored[0]["repeat"] == 3


def test_an_unknown_api_error_is_not_given_as_the_reason(acme, tmp_path):
    conn = db.get_connection()
    _errored_three_times(conn, tmp_path)

    problem = db.now_queue(conn)["problems"][0]
    events = db.list_events(conn, project_id=acme["id"])

    assert problem["reason"] == "Stopped on an API error (Claude Code didn't say which)"
    assert not any(e["summary"].endswith("unknown") for e in events)


def test_a_session_card_never_says_none(client, acme, tmp_path):
    _errored_three_times(db.get_connection(), tmp_path)

    text = client.get("/sessions").text

    assert "Claude Code didn&#39;t say which" in text or "Claude Code didn't say which" in text
    assert ">None<" not in text


def test_a_known_api_error_still_shows(acme, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "s-1", str(tmp_path))
    db.session_prompt_submitted(conn, "s-1", "go")
    db.session_stop_failure(conn, "s-1", "overloaded")

    assert db.now_queue(conn)["problems"][0]["reason"] == "Stopped on an API error: overloaded"


def test_recaps_lose_markdown_table_syntax(acme, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "s-1", str(tmp_path))
    table = "Commits so far:\n\n| Commit | What |\n|---|---|\n| b5562a5 | Route test fix |\n| afdc26f | ELK root cause |"
    db.record_session_summary(conn, "s-1", table)

    recap = db.list_sessions(conn)[0]["last_summary"]

    assert "|" not in recap and "---" not in recap
    assert "b5562a5 · Route test fix" in recap


def test_recaps_stored_before_the_fix_are_cleaned_for_display(acme, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "s-1", str(tmp_path))
    conn.execute("UPDATE sessions SET last_summary = 'Done: | Commit | What | |---|---| | abc | Fix |'")
    conn.commit()

    assert "|" not in db.list_sessions(conn)[0]["last_summary"]


def test_now_shows_the_recommendation_before_you_accept_it(client, acme):
    conn = db.get_connection()
    db.create_decision_request(conn, project_id=acme["id"], title="Queue", question="Which?",
                               recommendation="Postgres — it's already running", confidence=80)

    text = client.get("/").text

    assert "Accept: Postgres — it&#39;s already running" in text or "Accept: Postgres — it's already running" in text
    assert "· 80%" in text


def test_the_round_card_has_one_accept_button(client, acme):
    conn = db.get_connection()
    decision_id = db.create_decision_request(conn, project_id=acme["id"], title="Queue", question="Which?",
                                             recommendation="Postgres", confidence=80)

    text = client.get(f"/round?keys=r:{decision_id}").text

    assert text.count('value="recommendation"') == 1


def test_activity_page_collapses_repeats(client, acme, tmp_path):
    _errored_three_times(db.get_connection(), tmp_path)

    assert "×3" in client.get("/activity").text


def test_user_guide_is_served_in_the_dashboard(client):
    text = client.get("/guide").text

    assert "<h1" in text and "Keyboard shortcuts" in text
    assert 'href="/guide"' in client.get("/").text


def test_opening_answer_on_now_does_not_repeat_accept_or_done(client, acme):
    conn = db.get_connection()
    db.create_decision_request(conn, project_id=acme["id"], title="Queue", question="Which?",
                               recommendation="Postgres", confidence=80)
    db.create_action_request(conn, project_id=acme["id"], title="Add DNS", question="Add the record")

    text = client.get("/").text

    assert text.count('value="recommendation"') == 1
    assert text.count('value="done"') == 2  # the action's Done, and its note box's Confirm done


def test_a_rejected_answer_goes_back_to_the_page_with_the_reason(client, acme):
    conn = db.get_connection()
    decision_id = db.create_decision_request(conn, project_id=acme["id"], title="Queue", question="Which?",
                                             options=[{"label": "Postgres"}, {"label": "SQLite"}])

    resp = client.post(f"/decisions/{decision_id}/answer", data={"answer_text": "", "next": "/"},
                       follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/?error=")
    page = client.get(resp.headers["location"]).text
    assert 'class="alert tone-danger page-alert"' in page and "pick one of the quick answers" in page
    assert db.get_decision_request(conn, decision_id)["status"] == "open"
