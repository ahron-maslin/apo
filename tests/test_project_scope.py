"""One layout for everything under Projects: the sidebar picks the scope (All projects,
or one project) and the tabs pick the view (Overview · Work · Decisions · Agents ·
Activity). Changing project keeps the tab; changing tab keeps the scope."""
import re

import pytest
from fastapi.testclient import TestClient

from apo import db

TABS = ["Overview", "Work", "Decisions", "Agents", "Activity"]
SUFFIXES = ["", "/work", "/decisions", "/agents", "/activity"]


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def two(db_path, tmp_path):
    conn = db.get_connection()
    projects = []
    for name in ("acme", "beta"):
        folder = tmp_path / name
        folder.mkdir()
        project = db.get_or_create_project(conn, name)
        db.ensure_repository_for_project(conn, project["id"], str(folder))
        projects.append(project)
    return projects


def _nav(html, cls):
    return re.search(rf'<nav class="{cls}".*?</nav>', html, re.S).group(0)


def _current(nav):
    return re.search(r'aria-current="page"[^>]*>(.*?)</a>', nav, re.S).group(1)


def _all_paths(two):
    pid = two[0]["id"]
    return [f"/projects{s}" for s in SUFFIXES] + [f"/projects/{pid}{s}" for s in SUFFIXES]


def test_every_projects_page_has_the_sidebar_and_the_same_five_tabs(client, two):
    for path in _all_paths(two):
        html = client.get(path).text
        rail = _nav(html, "project-rail")
        assert "All projects" in rail and "acme" in rail and "beta" in rail, path
        tabs = _nav(html, "project-tabs")
        assert re.findall(r'title="(\w+) \(\d\)"', tabs) == TABS, path
        assert 'class="all-tabs"' not in html, path


def test_changing_project_keeps_the_tab(client, two):
    acme, beta = two

    rail = _nav(client.get(f"/projects/{acme['id']}/decisions").text, "project-rail")

    assert 'href="/projects/decisions"' in rail
    assert f'href="/projects/{beta["id"]}/decisions"' in rail
    assert "acme" in _current(rail)


def test_changing_tab_keeps_the_scope(client, two):
    acme, _ = two

    all_tabs = _nav(client.get("/projects/work").text, "project-tabs")
    one_tabs = _nav(client.get(f"/projects/{acme['id']}/agents").text, "project-tabs")

    assert re.findall(r'href="([^"]+)"', all_tabs) == [f"/projects{s}" for s in SUFFIXES]
    assert "Work" in _current(all_tabs)
    assert re.findall(r'href="([^"]+)"', one_tabs) == [f"/projects/{acme['id']}{s}" for s in SUFFIXES]
    assert "Agents" in _current(one_tabs)
    assert "All projects" in _current(_nav(client.get("/projects/work").text, "project-rail"))


def test_brackets_step_through_all_projects_and_each_project_on_the_same_tab(client, two):
    html = client.get("/projects/agents").text

    order = re.findall(r'href="/projects/(\d+)/agents"', _nav(html, "project-rail").split("<p")[0])
    assert re.search(rf'data-hotkey="\]" href="/projects/{order[0]}/agents"', html)
    assert re.search(rf'data-hotkey="\[" href="/projects/{order[-1]}/agents"', html)


def test_all_projects_overview_has_every_project_and_every_goal(client, two):
    conn = db.get_connection()
    db.create_outcome(conn, two[1]["id"], "Launch the beta")

    html = client.get("/projects").text

    assert f'href="/projects/{two[0]["id"]}"' in html and f'href="/projects/{two[1]["id"]}"' in html
    assert "Launch the beta" in html
    assert 'action="/outcomes"' in html and 'name="project"' in html


def test_all_projects_work_lists_everything_with_its_project(client, two):
    conn = db.get_connection()
    db.create_task(conn, project_id=two[0]["id"], title="Add rate limiting")
    db.create_decision_request(conn, project_id=two[1]["id"], title="Pick a queue", question="?")

    html = client.get("/projects/work").text

    assert "Add rate limiting" in html and "Pick a queue" in html
    assert re.search(r'Add rate limiting.*?acme', html, re.S) and re.search(r'Pick a queue.*?beta', html, re.S)
    assert 'action="/projects/items"' in html


def test_adding_work_from_all_projects_names_the_project(client, two):
    resp = client.post("/projects/items", data={"project": str(two[1]["id"]), "title": "Renew the domain", "owner": "me"},
                       follow_redirects=False)

    assert resp.status_code == 303
    assert [i["title"] for i in db.project_work(db.get_connection(), two[1]["id"])["needs_you"]] == ["Renew the domain"]


def test_all_projects_decisions_has_whats_waiting_and_the_log(client, two):
    conn = db.get_connection()
    db.create_decision_request(conn, project_id=two[0]["id"], title="Still open", question="?")
    done = db.create_decision_request(conn, project_id=two[1]["id"], title="Queue", question="Postgres or SQLite?")
    db.answer_decision_request(conn, done, "Postgres")

    html = client.get("/projects/decisions").text

    assert "Still open" in html and "Postgres or SQLite?" in html
    assert re.search(r'Queue.*?beta', html, re.S)


def test_all_projects_agents_is_the_live_fleet(client, two, tmp_path):
    db.register_session(db.get_connection(), "s-1", str(tmp_path / "acme"))

    html = client.get("/projects/agents").text

    assert 'id="session-card-' in html and 'data-stat="needs_you"' in html and 'id="fleet-sections"' in html


def test_all_projects_activity_has_no_project_dropdown(client, two):
    db.create_decision_request(db.get_connection(), project_id=two[0]["id"], title="Pick a queue", question="?")

    html = client.get("/projects/activity").text

    assert "Pick a queue" in html
    assert 'name="project"' not in _main(html)


def _main(html):
    return re.search(r'<main.*?</main>', html, re.S).group(0)


@pytest.mark.parametrize("old,new", [
    ("/outcomes", "/projects"), ("/sessions", "/projects/agents"), ("/decisions", "/projects/decisions"),
    ("/activity", "/projects/activity"),
])
def test_old_pages_redirect_to_their_all_projects_tab(client, two, old, new):
    resp = client.get(old, follow_redirects=False)

    assert resp.status_code in (302, 307, 308) and resp.headers["location"] == new


def test_old_links_to_one_project_land_on_that_projects_tab(client, two):
    acme = two[0]

    assert client.get("/decisions?project=acme", follow_redirects=False).headers["location"] == f"/projects/{acme['id']}/decisions"
    assert client.get("/activity?project=acme&all=1", follow_redirects=False).headers["location"] == f"/projects/{acme['id']}/activity?all=1"


def test_on_a_phone_the_sidebar_is_a_project_picker(client, two):
    html = client.get(f"/projects/{two[0]['id']}/work").text

    picker = re.search(r'<select[^>]*data-navigate.*?</select>', html, re.S).group(0)
    assert 'value="/projects/work"' in picker and f'value="/projects/{two[1]["id"]}/work"' in picker
