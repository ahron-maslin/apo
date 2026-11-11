"""A project's settings live behind the gear in its header: the purpose, status and
priority, how agents may work, its folders, Goals from Linear, and renaming, merging
and archiving. The project's own pages keep the work."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def acme(db_path):
    folder = Path.home() / "projects" / "acme"
    folder.mkdir(parents=True)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(folder))
    db.get_or_create_project(conn, "globex")
    return project


SETTINGS_FORMS = ("brief", "status", "autonomy", "rules", "rename", "merge", "archive")


def _actions(html, pid):
    return {a for a in SETTINGS_FORMS if f'action="/projects/{pid}/{a}"' in html}


def test_the_settings_page_holds_the_projects_configuration(client, acme):
    pid = acme["id"]

    html = client.get(f"/projects/{pid}/settings").text

    assert _actions(html, pid) == set(SETTINGS_FORMS)
    assert "~/projects/acme" in html and "default" in html
    assert re.search(r'action="/projects/%d/archive"[^>]*data-confirm=' % pid, html)


def test_the_project_pages_keep_the_work_and_drop_the_configuration(client, acme):
    pid = acme["id"]

    for tab in ("", "/work", "/decisions", "/agents", "/activity"):
        html = client.get(f"/projects/{pid}{tab}").text
        assert f'href="/projects/{pid}/settings"' in html, tab
        assert not _actions(html, pid) - {"rules"}, tab
    assert "Status and priority" not in client.get(f"/projects/{pid}").text


def test_the_header_shows_the_purpose_or_asks_for_one(client, acme):
    pid = acme["id"]
    assert "No purpose yet" in client.get(f"/projects/{pid}/work").text

    db.set_project_brief(db.get_connection(), pid, goal="Sell anvils online", links="Shop https://example.com")

    html = client.get(f"/projects/{pid}/work").text
    assert "Sell anvils online" in html and 'href="https://example.com"' in html and "No purpose yet" not in html


@pytest.mark.parametrize("path, data", [
    ("brief", {"goal": "Sell anvils"}),
    ("status", {"status": "at_risk", "priority": "hot"}),
    ("autonomy", {"autonomy": "edits"}),
    ("rules", {"text": "Use Postgres"}),
    ("linear-goals", {"enabled": "1"}),
    ("rename", {"name": "acme corp"}),
])
def test_saving_a_setting_comes_back_to_settings(client, acme, path, data):
    resp = client.post(f"/projects/{acme['id']}/{path}", data=data, follow_redirects=False)

    assert resp.status_code == 303 and resp.headers["location"].startswith(f"/projects/{acme['id']}/settings")


def test_retiring_a_rule_comes_back_to_settings(client, acme):
    rule = db.add_standing_rule(db.get_connection(), acme["id"], "Use Postgres")

    resp = client.post(f"/projects/{acme['id']}/rules/{rule}/retire", follow_redirects=False)

    assert resp.headers["location"].startswith(f"/projects/{acme['id']}/settings")


def test_moving_between_projects_from_settings_stays_on_settings(client, acme):
    globex = db.find_project(db.get_connection(), "globex")

    html = client.get(f"/projects/{acme['id']}/settings").text

    assert f'href="/projects/{globex["id"]}/settings"' in html
    assert 'href="/projects"' in html
