"""Cross-project 'This week' view and the outcome progress trend. Read-only over existing data."""
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo.web import app as app_module
from apo import db


@pytest.fixture
def conn(db_path):
    return db.get_connection()


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _days_ago_at_noon(days):
    """Noon local time, `days` calendar days back. The stall badge counts calendar days,
    and exactly days*24 hours back lands on a different date near midnight once the
    clocks have changed in between."""
    return datetime.combine(date.today() - timedelta(days=days), time(12)).astimezone().isoformat()


def _week():
    now = datetime.now().astimezone()
    stop = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()).astimezone()
    return stop - timedelta(days=7), stop


def test_week_overview_groups_finished_slipped_and_stuck_per_project(conn):
    acme = db.get_or_create_project(conn, "acme")["id"]
    beta = db.get_or_create_project(conn, "beta")["id"]
    done = db.create_task(conn, project_id=acme, title="Ship it")
    db.complete_task(conn, done, note="ok")
    late = db.create_decision_request(conn, project_id=acme, title="Late", question="Q?")
    conn.execute("UPDATE decision_requests SET due_on = ? WHERE id = ?", ("2000-01-01", late))
    waiting = db.create_decision_request(conn, project_id=beta, title="Pick", question="Q?")
    retry = db.create_task(conn, project_id=beta, title="Flaky")
    db._record_attempt(conn, retry, "retried")
    for sid, project, verdict in (("s1", acme, "done"), ("s2", acme, "partial"), ("s3", beta, "abandoned"),
                                  ("s4", beta, "crashed")):
        db.register_session(conn, sid, f"/tmp/{sid}")
        conn.execute("UPDATE sessions SET project_id = ?, status = 'ended', ended_at = ?, verdict = ? "
                     "WHERE claude_session_id = ?", (project, _ago(1), verdict, sid))
    conn.commit()
    start, stop = _week()

    week = db.week_overview(conn, start, stop)

    by = {p["project"]["name"]: p for p in week["projects"]}
    assert [i["id"] for i in by["acme"]["finished"]["items"]] == [done]
    assert sorted(s["claude_session_id"] for s in by["acme"]["finished"]["sessions"]) == ["s1", "s2"]
    assert [i["id"] for i in by["acme"]["slipped"]["overdue"]] == [late]
    assert sorted(s["claude_session_id"] for s in by["beta"]["slipped"]["sessions"]) == ["s3", "s4"]
    assert [i["id"] for i in by["beta"]["slipped"]["retried"]] == [retry]
    assert [i["id"] for i in by["beta"]["stuck"]] == [waiting]
    assert week["totals"]["finished"] == 3 and week["totals"]["stuck"] == 2 and week["totals"]["slipped"] == 4


def test_week_overview_counts_reopened_and_ignores_other_weeks(conn):
    p = db.get_or_create_project(conn, "acme")["id"]
    item = db.create_task(conn, project_id=p, title="Again")
    db.complete_task(conn, item)
    db.reopen_item(conn, item)
    old = db.create_task(conn, project_id=p, title="Old")
    db.complete_task(conn, old)
    conn.execute("UPDATE decision_requests SET resolved_at = ? WHERE id = ?", (_ago(30), old))
    conn.commit()
    start, stop = _week()

    acme = db.week_overview(conn, start, stop)["projects"][0]

    assert [i["id"] for i in acme["slipped"]["reopened"]] == [item]
    assert old not in [i["id"] for i in acme["finished"]["items"]]


def test_week_page_and_range_selector(db_path, conn):
    p = db.get_or_create_project(conn, "acme")["id"]
    t = db.create_task(conn, project_id=p, title="Ship it")
    db.complete_task(conn, t)
    client = TestClient(app_module.app)
    page = client.get("/projects/week")
    assert page.status_code == 200 and "Ship it" in page.text and "acme" in page.text
    assert "Ship it" not in client.get("/projects/week?week=last").text
    today = datetime.now().astimezone().date()
    assert "Ship it" in client.get(f"/projects/week?start={today - timedelta(days=2)}&end={today}").text
    assert client.get("/projects/week?start=nope").status_code == 400
    assert client.get("/projects/report").status_code == 200


def test_palette_and_overview_link_to_week(db_path):
    client = TestClient(app_module.app)
    assert "/projects/week" in [p["href"] for p in client.get("/api/palette").json()["pages"]]
    assert "/projects/week" in client.get("/projects").text


def test_outcome_trend_series_and_stall(conn):
    p = db.get_or_create_project(conn, "acme")["id"]
    o = db.create_outcome(conn, p, "Launch")
    a = db.add_step(conn, o, "one", "me")
    b = db.add_step(conn, o, "two", "me")
    db.add_step(conn, o, "three", "me")
    conn.execute("UPDATE outcomes SET created_at = ? WHERE id = ?", (_ago(20), o))
    conn.execute("UPDATE decision_requests SET created_at = ? WHERE outcome_id = ?", (_ago(20), o))
    conn.commit()
    db.complete_step(conn, a)
    db.complete_step(conn, b)
    conn.execute("UPDATE decision_requests SET answered_at = ? WHERE id = ?", (_ago(12), a))
    conn.execute("UPDATE decision_requests SET answered_at = ? WHERE id = ?", (_ago(10), b))
    conn.commit()

    trend = db.outcome_trend(conn, o, days=30)

    assert trend["done"][-1] == 2 and trend["total"][-1] == 3
    assert trend["done"] == sorted(trend["done"]) and len(trend["days"]) == len(trend["done"])
    assert trend["stalled_days"] in (10, 11)
    assert db.list_outcomes(conn)[0]["trend"]["stalled_days"] == trend["stalled_days"]


def test_no_stall_warning_when_recent_progress_paused_or_complete(conn):
    p = db.get_or_create_project(conn, "acme")["id"]
    o = db.create_outcome(conn, p, "Fresh")
    s = db.add_step(conn, o, "one", "me")
    db.add_step(conn, o, "two", "me")
    db.complete_step(conn, s)
    assert db.outcome_trend(conn, o)["stalled_days"] is None
    o2 = db.create_outcome(conn, p, "Old")
    db.add_step(conn, o2, "x", "me")
    conn.execute("UPDATE outcomes SET created_at = ? WHERE id = ?", (_ago(20), o2))
    conn.commit()
    assert db.outcome_trend(conn, o2)["stalled_days"] >= 19
    db.set_outcome_status(conn, o2, "paused")
    assert db.outcome_trend(conn, o2)["stalled_days"] is None


def test_outcome_page_and_goals_list_show_trend_and_warning(db_path, conn):
    p = db.get_or_create_project(conn, "acme")["id"]
    o = db.create_outcome(conn, p, "Stalled goal")
    db.add_step(conn, o, "x", "me")
    conn.execute("UPDATE outcomes SET created_at = ? WHERE id = ?", (_days_ago_at_noon(15), o))
    conn.commit()
    client = TestClient(app_module.app)
    text = client.get(f"/outcomes/{o}").text
    assert "<svg" in text and "burnup" in text
    for path in (f"/outcomes/{o}", "/projects", f"/projects/{p}"):
        assert "No progress for 15 days" in client.get(path).text, path
