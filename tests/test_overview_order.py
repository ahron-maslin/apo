"""A project's Overview reads top to bottom as goals, what needs you, what's ready to
merge, then what happened: no side column repeating the tab counts."""
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


def test_goals_then_what_needs_you_then_what_happened(client, acme):
    conn = db.get_connection()
    db.create_outcome(conn, acme["id"], "Launch the site")
    db.create_decision_request(conn, project_id=acme["id"], title="Pick a queue", question="?")

    html = client.get(f"/projects/{acme['id']}").text

    assert html.index('id="goals-heading"') < html.index('id="needs-heading"') < html.index('id="recent-heading"')
    assert "At a glance" not in html and 'class="detail-aside' not in html


def test_a_goal_is_one_compact_row(client, acme):
    conn = db.get_connection()
    outcome = db.create_outcome(conn, acme["id"], "Launch the site")
    db.add_step(conn, outcome, "Write the copy", owner="me")

    html = client.get(f"/projects/{acme['id']}").text
    card = html[html.index('class="outcome-card'):]
    card = card[:card.index("</form>")]

    assert "burnup" not in card and "Write the copy" in card and "owner-pill" in card and "0/1" in card


def test_the_header_says_when_it_was_last_active_without_repeating_the_counts(client, acme):
    db.create_decision_request(db.get_connection(), project_id=acme["id"], title="Pick a queue", question="?")

    html = client.get(f"/projects/{acme['id']}/work").text
    desc = html[html.index('class="page-desc"'):]
    desc = desc[:desc.index("</p>")]

    assert "active" in desc.lower() and "1 you" not in desc
