"""The Overview groups by purpose: what the project is for, its goals (and where they
come from), what needs you, how agents work here, and the project's own status and
priority in one place."""
import re

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


def _section(html, heading_id):
    return re.search(rf'<section[^>]*aria-labelledby="{heading_id}".*?</section>', html, re.S).group(0)


def _forms(html, action):
    return re.findall(rf'<form[^>]*action="{re.escape(action)}".*?</form>', html, re.S)


def test_the_brief_is_purpose_and_links_and_goals_own_done_when(client, acme):
    html = client.get(f"/projects/{acme['id']}").text

    [brief] = _forms(html, f"/projects/{acme['id']}/brief")
    assert 'name="goal"' in brief and 'name="links"' in brief and 'name="done_when"' not in brief


def test_how_agents_work_here_holds_the_permission_level_and_the_rules(client, acme):
    db.add_standing_rule(db.get_connection(), acme["id"], "Use Postgres for anything new.")

    section = _section(client.get(f"/projects/{acme['id']}").text, "agents-here-heading")

    assert "How agents work here" in section
    assert f'action="/projects/{acme["id"]}/autonomy"' in section
    assert f'action="/projects/{acme["id"]}/rules"' in section and "Use Postgres for anything new." in section


def test_status_and_priority_are_one_form(client, acme):
    html = client.get(f"/projects/{acme['id']}").text

    [form] = _forms(html, f"/projects/{acme['id']}/status")
    assert 'name="status"' in form and 'name="note"' in form and 'name="priority"' in form
    assert f'action="/projects/{acme["id"]}/priority"' not in html


def test_saving_status_and_priority_together(client, acme):
    resp = client.post(f"/projects/{acme['id']}/status", follow_redirects=False,
                       data={"status": "at_risk", "note": "Client is slow", "priority": "hot"})

    conn = db.get_connection()
    assert resp.status_code == 303
    assert db.project_status(conn, acme["id"])["value"] == "at_risk"
    assert db.project_priority(conn, acme["id"]) == "hot"


def test_goals_from_linear_sit_with_the_goals(client, acme):
    section = _section(client.get(f"/projects/{acme['id']}").text, "goals-heading")

    assert 'id="linear-goals"' in section
