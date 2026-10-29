"""Priority between projects (hot / normal / background) orders Now and decides who may
interrupt you; due dates on goals and work make lateness visible and count toward a
project's suggested status."""
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _project(conn, tmp_path, name):
    project = db.get_or_create_project(conn, name)
    (tmp_path / name).mkdir(exist_ok=True)
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path / name))
    return project


def _decision(conn, project, title, hours_ago=1):
    decision_id = db.create_decision_request(conn, project_id=project["id"], title=title, question="?")
    at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    conn.execute("UPDATE decision_requests SET created_at = ? WHERE id = ?", (at, decision_id))
    conn.commit()
    return decision_id


def _days(n):
    return (date.today() + timedelta(days=n)).isoformat()


def test_priority_is_hot_normal_or_background(db_path, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path, "acme")

    assert db.project_priority(conn, project["id"]) == "normal"
    db.set_project_priority(conn, project["id"], "hot")
    assert db.project_priority(conn, project["id"]) == "hot"
    with pytest.raises(ValueError):
        db.set_project_priority(conn, project["id"], "urgent")


def test_now_puts_hot_projects_first_and_background_last(db_path, tmp_path):
    conn = db.get_connection()
    hot, normal, quiet = (_project(conn, tmp_path, n) for n in ("hot", "normal", "quiet"))
    db.set_project_priority(conn, hot["id"], "hot")
    db.set_project_priority(conn, quiet["id"], "background")
    _decision(conn, quiet, "Background question", hours_ago=6)
    _decision(conn, normal, "Normal question", hours_ago=3)
    _decision(conn, hot, "Hot question", hours_ago=1)

    titles = [i["title"] for i in db.now_queue(conn)["your_move"]]

    assert titles == ["Hot question", "Normal question", "Background question"]


def test_hot_projects_interrupt_at_once_and_background_ones_never(db_path, tmp_path):
    conn = db.get_connection()
    db.set_ui_state(conn, "notify_hours", "0-24")
    hot, quiet = _project(conn, tmp_path, "hot"), _project(conn, tmp_path, "quiet")
    db.set_project_priority(conn, hot["id"], "hot")
    db.set_project_priority(conn, quiet["id"], "background")
    _decision(conn, hot, "Hot question", hours_ago=0)
    _decision(conn, quiet, "Background question", hours_ago=5)

    titles = [n["title"] for n in db.notifications_due(conn)["notify"]]

    assert any(t.startswith("Hot question") for t in titles)
    assert not any(t.startswith("Background question") for t in titles)


def test_setting_priority_from_the_overview(client, db_path, tmp_path):
    project = _project(db.get_connection(), tmp_path, "acme")

    resp = client.post(f"/projects/{project['id']}/status", data={"status": "", "priority": "hot"}, follow_redirects=False)
    html = client.get(f"/projects/{project['id']}").text

    assert resp.status_code == 303
    assert re.search(r'<option value="hot" selected', html)
    header = re.search(r'<header class="[^"]*project-header.*?</header>', html, re.S).group(0)
    assert "Hot" in header


def test_due_dates_on_goals_and_work(db_path, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path, "acme")
    outcome_id = db.create_outcome(conn, project["id"], "Launch", due_on=_days(5))
    task_id = db.create_task(conn, project_id=project["id"], title="Write the docs")

    db.set_due(conn, task_id, _days(-2))

    assert db.get_outcome(conn, outcome_id)["due_on"] == _days(5)
    assert db.get_decision_request(conn, task_id)["due_on"] == _days(-2)
    db.set_due(conn, task_id, None)
    assert db.get_decision_request(conn, task_id)["due_on"] is None
    with pytest.raises(ValueError):
        db.set_due(conn, task_id, "next tuesday")


def test_how_a_due_date_reads():
    from apo.web import app

    today = date.today()
    assert app._due(_days(-2), today) == {"label": "Overdue 2d", "tone": "danger", "overdue": True}
    assert app._due(_days(0), today)["label"] == "Due today"
    assert app._due(_days(1), today)["label"] == "Due tomorrow"
    assert app._due(_days(30), today)["tone"] == "neutral"
    assert app._due(None, today) is None


def test_overdue_work_puts_a_project_at_risk(db_path, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path, "acme")
    task_id = db.create_task(conn, project_id=project["id"], title="Write the docs")
    db.set_due(conn, task_id, _days(-1))

    status = db.project_status(conn, project["id"])

    assert status["suggested"] == "at_risk" and "overdue" in status["reason"]
    db.complete_task(conn, task_id)
    assert db.project_status(conn, project["id"])["suggested"] != "at_risk"


def test_an_overdue_goal_counts_too(db_path, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path, "acme")
    db.create_outcome(conn, project["id"], "Launch", due_on=_days(-3))

    assert db.project_status(conn, project["id"])["suggested"] == "at_risk"


def test_due_dates_show_on_goal_cards_work_and_the_item_page(client, db_path, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path, "acme")
    db.create_outcome(conn, project["id"], "Launch", due_on=_days(-1))
    task_id = db.create_task(conn, project_id=project["id"], title="Write the docs")
    db.set_due(conn, task_id, _days(1))

    assert "Overdue 1d" in client.get(f"/projects/{project['id']}").text
    assert "Due tomorrow" in client.get(f"/projects/{project['id']}/work").text
    page = client.get(f"/decisions/{task_id}").text
    assert re.search(rf'<form[^>]*action="/decisions/{task_id}/due"', page) and "Due tomorrow" in page


def test_setting_due_dates_from_the_dashboard(client, db_path, tmp_path):
    conn = db.get_connection()
    project = _project(conn, tmp_path, "acme")
    task_id = db.create_task(conn, project_id=project["id"], title="Write the docs")

    client.post(f"/decisions/{task_id}/due", data={"due_on": _days(3)})
    client.post("/outcomes", data={"project": "acme", "title": "Launch", "due_on": _days(7)})
    outcome = conn.execute("SELECT id FROM outcomes WHERE title = 'Launch'").fetchone()
    client.post(f"/outcomes/{outcome['id']}/due", data={"due_on": _days(10)})
    client.post(f"/projects/{project['id']}/items", data={"kind": "bug", "title": "Crash on save", "due_on": _days(2)})

    assert db.get_decision_request(conn, task_id)["due_on"] == _days(3)
    assert db.get_outcome(conn, outcome["id"])["due_on"] == _days(10)
    bug = conn.execute("SELECT due_on FROM decision_requests WHERE title = 'Crash on save'").fetchone()
    assert bug["due_on"] == _days(2)


def test_agents_see_priority_and_due_dates(db_path, tmp_path):
    from apo import mcp_server

    conn = db.get_connection()
    project = _project(conn, tmp_path, "acme")
    db.set_project_priority(conn, project["id"], "hot")
    db.create_outcome(conn, project["id"], "Launch", due_on=_days(4))

    assert mcp_server.get_project_brief("acme")["priority"] == "hot"
    assert mcp_server.list_outcomes("acme")[0]["due_on"] == _days(4)
