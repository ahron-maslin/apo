"""Projects are the home: the top bar is Now · Projects · Search · Settings. The
cross-project views (goals, agents, decisions, activity) sit under Projects as
"All projects" tabs, and a project's page has a rail to switch projects."""
import re

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _top_nav(html):
    return re.search(r'<nav class="app-nav".*?</nav>', html, re.S).group(0)


def test_the_top_bar_is_now_projects_settings_with_search_in_the_header(client):
    html = client.get("/settings").text

    assert re.findall(r'<a href="([^"]+)"', _top_nav(html)) == ["/", "/projects", "/settings"]
    assert re.search(r'<button[^>]*palette-trigger[^>]*data-palette-open', html)


@pytest.mark.parametrize("path,label", [("/projects", "Overview"), ("/outcomes", "Overview"), ("/sessions", "Agents"),
                                        ("/decisions", "Decisions"), ("/activity", "Activity")])
def test_the_old_cross_project_pages_land_on_their_all_projects_tab(client, path, label):
    html = client.get(path).text

    assert "Projects" in _top_nav(html).split('aria-current="page"')[1][:200]
    tabs = re.search(r'<nav class="project-tabs".*?</nav>', html, re.S).group(0)
    assert label in re.search(r'aria-current="page"[^>]*>(.*?)</a>', tabs, re.S).group(1)


def test_search_is_current_on_the_search_page(client):
    html = client.get("/search").text

    assert re.search(r'<button[^>]*palette-trigger[^>]*aria-current="page"', html)


def test_a_project_page_has_a_rail_to_switch_projects(client, tmp_path):
    conn = db.get_connection()
    ids = []
    for name in ("alpha", "beta", "gamma"):
        folder = tmp_path / name
        folder.mkdir()
        project = db.get_or_create_project(conn, name)
        db.ensure_repository_for_project(conn, project["id"], str(folder))
        ids.append(project["id"])

    html = client.get(f"/projects/{ids[1]}/work").text

    rail = re.search(r'<nav class="project-rail".*?</nav>', html, re.S).group(0)
    assert all(f'href="/projects/{i}/work"' in rail for i in ids)
    assert re.search(r'data-hotkey="\[" href="/projects/\d+/work"', html)
    assert re.search(r'data-hotkey="\]" href="/projects/\d+/work"', html)
