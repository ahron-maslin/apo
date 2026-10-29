"""Interruptions are a budget, not a firehose. Only a move that's waited a while
(or sits in a pinned project) interrupts; at most 6 a day, 20 minutes apart, one
per project per hour, inside active hours, never while you're mid-prompt. One
reminder at 4h, then it only ranks. A move that's gone retracts its notification."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(db_path):
    conn = db.get_connection()
    db.set_ui_state(conn, "notify_hours", "0-24")
    return conn


def _project(conn, name, tmp_path):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    project = db.get_or_create_project(conn, name)
    db.ensure_repository_for_project(conn, project["id"], str(folder))
    return project


def _decision(conn, project, title, waited_minutes=45):
    decision_id = db.create_decision_request(conn, project_id=project["id"], title=title, question=f"{title}?")
    conn.execute("UPDATE decision_requests SET created_at = ? WHERE id = ?",
                 ((NOW - timedelta(minutes=waited_minutes)).isoformat(), decision_id))
    conn.commit()
    return decision_id


def _sent(conn, at=NOW):
    return [n["key"] for n in db.notifications_due(conn, now=at)["notify"]]


def test_a_move_that_has_waited_half_an_hour_interrupts_once(conn, tmp_path):
    decision_id = _decision(conn, _project(conn, "acme", tmp_path), "Pick a queue")

    assert _sent(conn) == [f"r:{decision_id}"]
    assert _sent(conn, NOW + timedelta(minutes=30)) == []


def test_a_fresh_move_waits_unless_its_project_is_pinned(conn, tmp_path):
    acme = _project(conn, "acme", tmp_path)
    decision_id = _decision(conn, acme, "Pick a queue", waited_minutes=5)
    assert _sent(conn) == []

    db.set_project_pinned(conn, acme["id"], True)

    assert _sent(conn) == [f"r:{decision_id}"]


def test_interrupts_are_spaced_twenty_minutes_apart(conn, tmp_path):
    _decision(conn, _project(conn, "acme", tmp_path), "One")
    _decision(conn, _project(conn, "beta", tmp_path), "Two")

    assert len(_sent(conn)) == 1
    assert _sent(conn, NOW + timedelta(minutes=10)) == []
    assert len(_sent(conn, NOW + timedelta(minutes=21))) == 1


def test_one_interrupt_per_project_per_hour(conn, tmp_path):
    acme = _project(conn, "acme", tmp_path)
    _decision(conn, acme, "One")
    _decision(conn, acme, "Two")

    assert len(_sent(conn)) == 1
    assert _sent(conn, NOW + timedelta(minutes=30)) == []
    assert len(_sent(conn, NOW + timedelta(minutes=61))) == 1


def test_at_most_six_a_day_and_the_rest_are_counted_as_held(conn, tmp_path):
    for n in range(8):
        _decision(conn, _project(conn, f"p{n}", tmp_path), f"Move {n}")

    sent = []
    for step in range(8):
        sent += _sent(conn, NOW + timedelta(minutes=21 * step))

    assert len(sent) == 6
    assert db.notifications_due(conn, now=NOW + timedelta(minutes=200))["held"] == 2


def test_nothing_outside_active_hours(conn, tmp_path):
    _decision(conn, _project(conn, "acme", tmp_path), "Pick a queue")
    hour = NOW.astimezone().hour
    db.set_ui_state(conn, "notify_hours", f"{(hour + 1) % 24}-{(hour + 2) % 24}")

    assert _sent(conn) == []


def test_nothing_while_you_are_mid_prompt_in_a_session(conn, tmp_path):
    acme = _project(conn, "acme", tmp_path)
    _decision(conn, acme, "Pick a queue")
    db.register_session(conn, "s-1", str(tmp_path / "acme"))
    db.session_prompt_submitted(conn, "s-1", "keep going")
    conn.execute("UPDATE sessions SET reviewed_at = ?", ((NOW - timedelta(minutes=1)).isoformat(),))
    conn.commit()

    assert _sent(conn) == []


def test_one_reminder_after_four_hours_then_it_only_ranks(conn, tmp_path):
    decision_id = _decision(conn, _project(conn, "acme", tmp_path), "Pick a queue")

    assert _sent(conn) == [f"r:{decision_id}"]
    assert _sent(conn, NOW + timedelta(hours=2)) == []
    assert _sent(conn, NOW + timedelta(hours=4, minutes=1)) == [f"r:{decision_id}"]
    assert _sent(conn, NOW + timedelta(hours=9)) == []


def test_a_handled_move_retracts_its_notification(conn, tmp_path):
    decision_id = _decision(conn, _project(conn, "acme", tmp_path), "Pick a queue")
    _sent(conn)

    db.answer_decision_request(conn, decision_id, "Postgres")

    due = db.notifications_due(conn, now=NOW + timedelta(minutes=1))
    assert due["retract"] == [f"r:{decision_id}"]
    assert db.notifications_due(conn, now=NOW + timedelta(minutes=2))["retract"] == []


def test_api_notify(conn, tmp_path):
    from apo.web import app

    decision_id = _decision(conn, _project(conn, "acme", tmp_path), "Pick a queue", waited_minutes=60 * 24)

    data = TestClient(app.app).get("/api/notify").json()

    assert [n["key"] for n in data["notify"]] == [f"r:{decision_id}"]
    note = data["notify"][0]
    assert note["href"] == f"/decisions/{decision_id}"
    assert "Pick a queue" in note["title"] and "acme" in note["title"]


def test_active_hours_are_set_in_settings(conn):
    from apo.web import app

    client = TestClient(app.app)
    assert "data-notify-toggle" in client.get("/settings").text

    client.post("/settings/notify-hours", data={"start": "9", "end": "18"})

    assert db.get_ui_state(conn, "notify_hours") == "9-18"
    assert client.post("/settings/notify-hours", data={"start": "9", "end": "30"}).status_code == 400
