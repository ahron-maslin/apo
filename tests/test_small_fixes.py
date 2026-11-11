"""Counts that agree with each other, words as they appear elsewhere, and links that
show what they name."""
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
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return project


def test_all_projects_says_how_many_agents_are_running_and_how_many_are_not(client, acme, tmp_path):
    conn = db.get_connection()
    for sid in ("s-busy", "s-idle"):
        db.register_session(conn, sid, str(tmp_path))
    conn.execute("UPDATE sessions SET exec_state = 'working' WHERE claude_session_id = 's-busy'")
    conn.commit()
    states = {s["claude_session_id"]: s["display_status"] for s in db.list_sessions(conn)}
    running = sum(1 for v in states.values() if v == "running")

    desc = re.search(r'<p class="page-desc">(.*?)</p>', client.get("/projects").text, re.S).group(1)

    assert f"{running} running" in desc and f"{len(states) - running} idle or waiting" in desc


def test_the_agents_page_links_to_what_waits_on_you_with_the_decisions_tabs_count(client, acme, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "s-1", str(tmp_path))
    db.create_decision_request(conn, project_id=acme["id"], title="Pick a queue", question="?")
    db.add_work(conn, acme["id"], "me", "Renew the domain")
    db.create_task(conn, project_id=acme["id"], title="Write the migration")

    html = client.get("/projects/agents").text

    assert "2 waiting on you" in html and "open decision" not in html


def test_now_counts_what_its_badge_counts(client, acme):
    db.create_decision_request(db.get_connection(), project_id=acme["id"], title="Pick a queue", question="?")
    html = client.get("/").text

    badge = re.search(r'data-nav-count="now">(\d+)<', html).group(1)
    assert re.search(rf'now-summary"[^>]*>\s*{badge} waiting on you', html)


def test_since_you_were_away_links_each_count_to_its_items(client, acme):
    conn = db.get_connection()
    db.set_ui_state(conn, "last_visit_seq", str(db.latest_event_seq(conn)))
    db.set_ui_state(conn, "last_visit_at", (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
    task = db.create_task(conn, project_id=acme["id"], title="Write the migration")
    at = (datetime.now(timezone.utc) - timedelta(days=db.STUCK_ITEM_DAYS + 1, hours=1)).isoformat()
    conn.execute("UPDATE decision_requests SET created_at = ? WHERE id = ?", (at, task))
    conn.commit()

    html = client.get("/").text

    assert re.search(r'href="/projects/work\?due=stuck">\d+ stuck</a>', html)


def test_a_goal_page_says_goal_and_a_task_page_is_under_work(client, acme, tmp_path):
    conn = db.get_connection()
    outcome = db.create_outcome(conn, acme["id"], "Launch the site")
    task = db.create_task(conn, project_id=acme["id"], title="Write the migration")

    goal = client.get(f"/outcomes/{outcome}").text
    assert re.search(r'class="eyebrow">Goal · Active', goal) and "Outcome ·" not in goal

    crumbs = re.search(r'<nav class="breadcrumb".*?</nav>', client.get(f"/decisions/{task}").text, re.S).group(0)
    assert 'href="/projects/work">Work<' in crumbs and f'href="/projects/{acme["id"]}/work"' in crumbs
