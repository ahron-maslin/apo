"""A weekly report per project: what got done and decided, what's blocked on you, how
long agents waited on you, and throughput. Built from the database (and git for code),
so the same week always reads the same. Printable and copyable as Markdown."""
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _at(days_ago=0, hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def _set(conn, item_id, **cols):
    conn.execute(f"UPDATE decision_requests SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                 (*cols.values(), item_id))
    conn.commit()


@pytest.fixture
def week(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    (tmp_path / "acme").mkdir()
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path / "acme"))
    pid = project["id"]

    done = db.create_task(conn, project_id=pid, title="Add rate limiting")
    db.complete_task(conn, done, note="Shipped")
    _set(conn, done, created_at=_at(days_ago=3), resolved_at=_at(days_ago=2))

    old = db.create_task(conn, project_id=pid, title="Done long ago")
    db.complete_task(conn, old)
    _set(conn, old, created_at=_at(days_ago=20), resolved_at=_at(days_ago=20))

    decided = db.create_decision_request(conn, project_id=pid, title="Pick a queue", question="Postgres or SQLite?",
                                         requested_by="agent")
    db.answer_decision_request(conn, decided, "Postgres")
    _set(conn, decided, created_at=_at(days_ago=1, hours_ago=2), answered_at=_at(days_ago=1))

    blocked = db.create_decision_request(conn, project_id=pid, title="Rotate the key", question="Now or later?",
                                         requested_by="agent")
    _set(conn, blocked, created_at=_at(days_ago=3))

    goal = db.create_outcome(conn, pid, "Launch the beta")
    db.set_outcome_status(conn, goal, "achieved")
    return project


def test_a_week_of_one_project(week):
    conn = db.get_connection()
    end = datetime.now(timezone.utc)

    report = db.weekly_report(conn, week["id"], end - timedelta(days=7), end)

    assert [i["title"] for i in report["done"]] == ["Add rate limiting"]
    assert [(d["title"], d["answer_text"]) for d in report["decided"]] == [("Pick a queue", "Postgres")]
    assert [g["title"] for g in report["goals_achieved"]] == ["Launch the beta"]
    assert [b["title"] for b in report["blocked"]] == ["Rotate the key"]
    assert report["filed"] == 3
    assert 70 <= report["waited_hours"] <= 76
    assert report["median_answer_minutes"] == 120
    assert len(report["trend"]) == 4 and report["trend"][-1] == 1


def test_a_week_with_nothing_is_empty_not_an_error(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "quiet")
    end = datetime.now(timezone.utc)

    report = db.weekly_report(conn, project["id"], end - timedelta(days=7), end)

    assert report["done"] == [] and report["decided"] == [] and report["waited_hours"] == 0
    assert report["median_answer_minutes"] is None


def test_the_report_page(client, week):
    html = client.get(f"/projects/{week['id']}/report").text

    assert "Add rate limiting" in html and "Pick a queue" in html and "Postgres" in html
    assert "Rotate the key" in html and "Launch the beta" in html
    assert "Done long ago" not in html
    copy = re.search(r'data-copy="([^"]*)"', html).group(1)
    assert "## acme" in copy and "Add rate limiting" in copy


def test_moving_between_weeks(client, week):
    today = date.today()
    html = client.get(f"/projects/{week['id']}/report").text

    prev_end = (today - timedelta(days=7)).isoformat()
    assert f'href="/projects/{week["id"]}/report?end={prev_end}"' in html
    earlier = client.get(f"/projects/{week['id']}/report?end={prev_end}").text
    assert "Add rate limiting" not in earlier
    assert client.get(f"/projects/{week['id']}/report?end=not-a-date").status_code == 400


def test_all_projects_report(client, week, tmp_path):
    db.get_or_create_project(db.get_connection(), "beta")

    html = client.get("/projects/report").text

    assert "acme" in html and "beta" in html and "Add rate limiting" in html


def test_the_overview_links_to_the_report(client, week):
    assert f'href="/projects/{week["id"]}/report"' in client.get(f"/projects/{week['id']}").text
