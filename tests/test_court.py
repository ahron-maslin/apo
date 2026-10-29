"""Time in your court: how long agents waited on you. Your answer latency is the
real bottleneck, so it's shown — once, plainly, without nagging."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apo import db

NOON = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)


@pytest.fixture
def acme(db_path):
    return db.get_or_create_project(db.get_connection(), "acme")


def _item(conn, project, title, asked_hours_ago, answered_hours_ago=None, **fields):
    decision_id = db.create_decision_request(conn, project_id=project["id"], title=title, question="?", **fields)
    asked = NOON - timedelta(hours=asked_hours_ago)
    conn.execute("UPDATE decision_requests SET created_at = ? WHERE id = ?", (asked.isoformat(), decision_id))
    if answered_hours_ago is not None:
        db.answer_decision_request(conn, decision_id, "ok")
        answered = NOON - timedelta(hours=answered_hours_ago)
        conn.execute("UPDATE decision_requests SET answered_at = ? WHERE id = ?", (answered.isoformat(), decision_id))
    conn.commit()
    return decision_id


def test_waited_today_counts_only_the_part_of_each_wait_since_midnight(acme):
    conn = db.get_connection()
    _item(conn, acme, "Answered", asked_hours_ago=3, answered_hours_ago=2)   # 1h
    _item(conn, acme, "Still open", asked_hours_ago=2)                       # 2h
    _item(conn, acme, "From yesterday", asked_hours_ago=20)                  # 12h of it today

    metrics = db.court_metrics(conn, now=NOON)

    assert metrics["waited_today_minutes"] == 60 + 120 + 12 * 60
    assert metrics["moves_waited_today"] == 3


def test_the_oldest_move_you_hold(acme):
    conn = db.get_connection()
    old = _item(conn, acme, "Newsletter copy", asked_hours_ago=26)
    _item(conn, acme, "Fresh", asked_hours_ago=2)

    oldest = db.court_metrics(conn, now=NOON)["oldest"]

    assert oldest["id"] == old and oldest["title"] == "Newsletter copy" and oldest["hours"] == 26


def test_median_answer_time_this_week(acme):
    conn = db.get_connection()
    _item(conn, acme, "a", asked_hours_ago=5, answered_hours_ago=4)     # 1h
    _item(conn, acme, "b", asked_hours_ago=6, answered_hours_ago=4)     # 2h
    _item(conn, acme, "c", asked_hours_ago=14, answered_hours_ago=4)    # 10h
    _item(conn, acme, "old", asked_hours_ago=24 * 9, answered_hours_ago=24 * 8)

    metrics = db.court_metrics(conn, now=NOON)

    assert metrics["median_answer_minutes"] == 120
    assert metrics["answered_this_week"] == 3


def test_only_agents_waiting_count(acme):
    conn = db.get_connection()
    _item(conn, acme, "My own todo", asked_hours_ago=5, requested_by="human", kind="action")
    _item(conn, acme, "Agent's task", asked_hours_ago=5, kind="task")

    metrics = db.court_metrics(conn, now=NOON)

    assert metrics["waited_today_minutes"] == 0
    assert metrics["oldest"] is None


def test_now_shows_it(db_path, acme):
    from apo.web import app

    _item(db.get_connection(), acme, "Newsletter copy", asked_hours_ago=26)

    text = TestClient(app.app).get("/").text

    assert "Agents waited" in text and "across 1 move" in text
    assert "Newsletter copy" in text


def test_waits_add_up_in_agent_hours_never_days():
    from apo.web import app

    assert app._hours(7251) == "121h"
    assert app._hours(20) == "20m"
