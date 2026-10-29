"""Snooze: personal, not a status. A snoozed request isn't answered or closed — it's
just out of the queue until a time, or until its agent actually blocks on it."""
from datetime import datetime, timedelta, timezone

import pytest

from apo import db


def _request(conn, tmp_path, origin=None):
    project = db.get_or_create_project(conn, "acme")
    repo_id = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo_id, title="Pick a queue", question="Q?",
        origin_session_id=origin,
    )


def _keys(items):
    return [i["key"] for i in items]


def test_a_snoozed_request_leaves_the_queue_and_comes_back_after(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _request(conn, tmp_path)

    db.snooze_request(conn, decision_id, until=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
    now = db.now_queue(conn)
    assert f"r:{decision_id}" not in _keys(now["your_move"])
    assert _keys(now["snoozed"]) == [f"r:{decision_id}"]
    assert now["quiet"]["snoozed"] == 1

    conn.execute("UPDATE decision_requests SET snoozed_until = ?", ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
    conn.commit()
    assert f"r:{decision_id}" in _keys(db.now_queue(conn)["your_move"])


def test_snooze_until_it_blocks_hides_it_only_while_the_agent_works(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "agent", str(tmp_path))
    decision_id = _request(conn, tmp_path, origin="agent")
    db.session_heartbeat(conn, "agent")

    db.snooze_request(conn, decision_id, condition="until_blocks")
    now = db.now_queue(conn)
    assert f"r:{decision_id}" not in _keys(now["asks"]) + _keys(now["your_move"])

    db.session_stop(conn, "agent")
    assert f"r:{decision_id}" in _keys(db.now_queue(conn)["your_move"])


def test_unsnoozing_puts_it_straight_back(db_path, tmp_path):
    conn = db.get_connection()
    decision_id = _request(conn, tmp_path)
    db.snooze_request(conn, decision_id, until=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())

    db.snooze_request(conn, decision_id)

    assert f"r:{decision_id}" in _keys(db.now_queue(conn)["your_move"])


def test_snooze_presets_land_on_sensible_local_times():
    local = datetime(2026, 10, 7, 14, 30).astimezone()  # a Wednesday afternoon

    def at(preset):
        return datetime.fromisoformat(db.snooze_preset_until(preset, now=local)).astimezone(local.tzinfo)

    assert at("2h") == local + timedelta(hours=2)
    assert (at("tonight").date(), at("tonight").hour) == (local.date(), 18)
    assert (at("tomorrow").date(), at("tomorrow").hour) == (local.date() + timedelta(days=1), 9)
    assert at("week").weekday() == 0 and at("week").hour == 9 and at("week") > local


def test_tonight_after_six_means_tomorrow_morning():
    local = datetime(2026, 10, 7, 19, 0).astimezone()
    until = datetime.fromisoformat(db.snooze_preset_until("tonight", now=local)).astimezone(local.tzinfo)
    assert (until.date(), until.hour) == (local.date() + timedelta(days=1), 9)


# Berlin falls back at 03:00 on Sunday 2026-10-25; New York springs forward at
# 02:00 on Sunday 2026-03-08. Each "now" is before the change, each preset after it.
@pytest.mark.parametrize("zone_name,now,preset,expected", [
    ("Europe/Berlin", datetime(2026, 10, 24, 20, 0), "tonight", datetime(2026, 10, 25, 9, 0)),
    ("Europe/Berlin", datetime(2026, 10, 24, 20, 0), "tomorrow", datetime(2026, 10, 25, 9, 0)),
    ("Europe/Berlin", datetime(2026, 10, 24, 20, 0), "week", datetime(2026, 10, 26, 9, 0)),
    ("Europe/Berlin", datetime(2026, 10, 25, 0, 30), "tonight", datetime(2026, 10, 25, 18, 0)),
    ("America/New_York", datetime(2026, 3, 7, 20, 0), "tonight", datetime(2026, 3, 8, 9, 0)),
    ("America/New_York", datetime(2026, 3, 7, 20, 0), "tomorrow", datetime(2026, 3, 8, 9, 0)),
    ("America/New_York", datetime(2026, 3, 7, 20, 0), "week", datetime(2026, 3, 9, 9, 0)),
    ("America/New_York", datetime(2026, 3, 8, 0, 30), "tonight", datetime(2026, 3, 8, 18, 0)),
])
def test_snooze_presets_keep_their_local_time_across_a_clock_change(local_zone, zone_name, now, preset, expected):
    zone = local_zone(zone_name)

    until = datetime.fromisoformat(db.snooze_preset_until(preset, now=now.replace(tzinfo=zone)))

    assert until.astimezone(zone).replace(tzinfo=None) == expected


@pytest.mark.parametrize("zone_name,now", [
    ("Europe/Berlin", datetime(2026, 10, 25, 0, 30)),
    ("America/New_York", datetime(2026, 3, 8, 0, 30)),
])
def test_two_hours_is_two_real_hours_even_when_the_clock_changes_inside_them(local_zone, zone_name, now):
    start = now.replace(tzinfo=local_zone(zone_name))

    until = datetime.fromisoformat(db.snooze_preset_until("2h", now=start))

    assert until - start.astimezone(timezone.utc) == timedelta(hours=2)


def test_snooze_route_and_now_page(db_path, tmp_path):
    from fastapi.testclient import TestClient

    from apo.web import app

    client = TestClient(app.app)
    conn = db.get_connection()
    db.register_session(conn, "agent", str(tmp_path))
    decision_id = _request(conn, tmp_path, origin="agent")
    db.session_stop(conn, "agent")
    assert 'value="until_blocks"' in client.get("/").text

    resp = client.post(f"/decisions/{decision_id}/snooze", data={"preset": "tomorrow", "next": "/"}, follow_redirects=False)

    assert resp.status_code == 303 and resp.headers["location"] == "/"
    assert db.get_decision_request(db.get_connection(), decision_id)["snoozed_until"]
    assert "Snoozed (1)" in client.get("/").text

    client.post(f"/decisions/{decision_id}/snooze", data={"preset": "clear"})
    assert db.get_decision_request(db.get_connection(), decision_id)["snoozed_until"] is None
