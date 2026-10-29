"""Every item a human creates on the dashboard lands in a folder. An agent only sees
work through a repo's .claude/decisions/INDEX.md, so an item saved with no folder in a
project with several is invisible to every session. The forms offer the project's
folders, default to the one last worked in, and an item can be moved later."""
import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from apo import db

from test_cli import run_cli


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def globex(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "Globex")
    repos = []
    for name in ("acme-shop", "dashboard"):
        folder = tmp_path / name
        folder.mkdir()
        repos.append(db.create_repository(conn, project["id"], name, str(folder)))
    return project, repos


def _index(repo):
    path = os.path.join(repo["local_path"], ".claude", "decisions", "INDEX.md")
    return open(path).read() if os.path.exists(path) else ""


def _item(title):
    row = db.get_connection().execute("SELECT id FROM decision_requests WHERE title = ?", (title,)).fetchone()
    return db.get_decision_request(db.get_connection(), row["id"])


def test_the_default_folder_is_where_you_last_worked(globex):
    project, (root, dash) = globex
    conn = db.get_connection()

    assert db.default_repository(conn, project["id"])["id"] == root["id"]
    db.register_session(conn, "s-1", dash["local_path"])
    assert db.default_repository(conn, project["id"])["id"] == dash["id"]


def test_adding_work_saves_the_folder_you_picked(client, globex):
    project, (root, dash) = globex

    client.post(f"/projects/{project['id']}/items",
                data={"title": "Station Review", "kind": "task", "repository": str(dash["id"])})

    assert _item("Station Review")["repository_id"] == dash["id"]
    assert re.search(r"^\| READY \| .*Station Review", _index(dash), re.M)
    assert "Station Review" not in _index(root)


def test_adding_work_without_a_pick_uses_the_default_never_no_folder(client, globex):
    project, (root, _) = globex

    client.post(f"/projects/{project['id']}/items", data={"title": "Renew the domain", "kind": "me"})
    client.post(f"/projects/{project['id']}/items", data={"title": "Add rate limiting", "kind": "task"})

    assert _item("Renew the domain")["repository_id"] == root["id"]
    assert _item("Add rate limiting")["repository_id"] == root["id"]


def test_a_folder_from_another_project_is_refused(client, globex, tmp_path):
    project, _ = globex
    conn = db.get_connection()
    other = db.get_or_create_project(conn, "acme")
    (tmp_path / "acme").mkdir()
    foreign = db.create_repository(conn, other["id"], "acme", str(tmp_path / "acme"))

    resp = client.post(f"/projects/{project['id']}/items",
                       data={"title": "Sneaky", "kind": "task", "repository": str(foreign["id"])},
                       follow_redirects=False)

    assert "error=" in resp.headers["location"]
    assert db.get_connection().execute("SELECT COUNT(*) FROM decision_requests").fetchone()[0] == 0


def test_the_work_tab_offers_a_folder_only_when_there_is_a_choice(client, globex, tmp_path):
    project, (root, dash) = globex
    db.register_session(db.get_connection(), "s-1", dash["local_path"])

    html = client.get(f"/projects/{project['id']}/work").text
    [picker] = re.findall(r'<select[^>]*name="repository".*?</select>', html, re.S)
    assert f'value="{root["id"]}"' in picker
    assert re.search(rf'value="{dash["id"]}"[^>]* selected', picker)

    conn = db.get_connection()
    solo = db.get_or_create_project(conn, "solo")
    (tmp_path / "solo").mkdir()
    db.create_repository(conn, solo["id"], "solo", str(tmp_path / "solo"))
    assert 'name="repository"' not in client.get(f"/projects/{solo['id']}/work").text


def test_all_projects_work_form_offers_each_folder(client, globex):
    project, (root, dash) = globex

    html = client.get("/projects/work").text
    picker = re.search(r'<select[^>]*name="project".*?</select>', html, re.S).group(0)
    assert f'value="{project["id"]}:{root["id"]}"' in picker and f'value="{project["id"]}:{dash["id"]}"' in picker

    client.post("/projects/items", data={"project": f"{project['id']}:{dash['id']}", "title": "Station Review"})
    assert _item("Station Review")["repository_id"] == dash["id"]


def test_a_bug_goes_to_the_folder_you_picked(client, globex):
    project, (_, dash) = globex

    client.post(f"/projects/{project['id']}/items", data={"kind": "bug", "title": "Map is blank", "repository": str(dash["id"])})

    assert _item("Map is blank")["repository_id"] == dash["id"]


def test_a_bug_without_a_pick_uses_the_default(client, globex):
    project, (root, _) = globex

    client.post(f"/projects/{project['id']}/items", data={"kind": "bug", "title": "Map is blank"})

    assert _item("Map is blank")["repository_id"] == root["id"]


def test_a_goal_step_goes_to_the_folder_you_picked(client, globex):
    project, (root, dash) = globex
    outcome_id = db.create_outcome(db.get_connection(), project["id"], "Ship the station")

    html = client.get(f"/outcomes/{outcome_id}").text
    assert re.search(r'<select[^>]*name="repository"', html)
    client.post(f"/outcomes/{outcome_id}/steps", data={"title": "Wire the map", "owner": "agent",
                                                      "repository": str(dash["id"])})
    client.post(f"/outcomes/{outcome_id}/steps", data={"title": "Pick a vendor", "owner": "me"})

    assert _item("Wire the map")["repository_id"] == dash["id"]
    assert _item("Pick a vendor")["repository_id"] == root["id"]


def test_moving_an_item_rewrites_both_folders(globex):
    project, (root, dash) = globex
    conn = db.get_connection()
    task_id = db.create_task(conn, project_id=project["id"], title="Station Review", repository_id=root["id"])
    assert "Station Review" in _index(root)

    db.move_item(conn, task_id, dash["id"])

    assert db.get_decision_request(conn, task_id)["repository_id"] == dash["id"]
    assert "Station Review" in _index(dash) and "Station Review" not in _index(root)
    assert not [f for f in os.listdir(os.path.join(root["local_path"], ".claude", "decisions")) if f.startswith(f"{task_id:04d}-")]


def test_an_item_with_no_folder_can_be_given_one(globex):
    project, (_, dash) = globex
    conn = db.get_connection()
    task_id = db.create_task(conn, project_id=project["id"], title="Station Review")
    assert db.get_decision_request(conn, task_id)["repository_id"] is None

    db.move_item(conn, task_id, dash["id"])

    assert re.search(r"^\| READY \| .*Station Review", _index(dash), re.M)


def test_moving_to_another_projects_folder_is_refused(globex, tmp_path):
    project, (root, _) = globex
    conn = db.get_connection()
    other = db.get_or_create_project(conn, "acme")
    (tmp_path / "acme").mkdir()
    foreign = db.create_repository(conn, other["id"], "acme", str(tmp_path / "acme"))
    task_id = db.create_task(conn, project_id=project["id"], title="Station Review", repository_id=root["id"])

    with pytest.raises(ValueError):
        db.move_item(conn, task_id, foreign["id"])
    assert db.get_decision_request(conn, task_id)["repository_id"] == root["id"]


def test_an_items_page_lets_you_move_it_and_warns_when_it_has_no_folder(client, globex):
    project, (root, dash) = globex
    conn = db.get_connection()
    task_id = db.create_task(conn, project_id=project["id"], title="Station Review")

    html = client.get(f"/decisions/{task_id}").text
    assert "No folder" in html
    form = re.search(rf'<form[^>]*action="/decisions/{task_id}/repository".*?</form>', html, re.S).group(0)
    assert f'value="{root["id"]}"' in form and f'value="{dash["id"]}"' in form

    resp = client.post(f"/decisions/{task_id}/repository", data={"repository": str(dash["id"])}, follow_redirects=False)

    assert resp.status_code == 303
    assert db.get_decision_request(conn, task_id)["repository_id"] == dash["id"]
    assert "No folder" not in client.get(f"/decisions/{task_id}").text


def test_moving_from_the_cli_by_path_or_name(db_path, globex):
    project, (root, dash) = globex
    conn = db.get_connection()
    env = dict(os.environ, APO_DB=str(db_path))
    task_id = db.create_task(conn, project_id=project["id"], title="Station Review")

    by_path = run_cli("move", str(task_id), dash["local_path"], env=env)
    assert by_path.returncode == 0, by_path.stderr
    assert db.get_decision_request(conn, task_id)["repository_id"] == dash["id"]

    by_name = run_cli("move", str(task_id), "acme-shop", env=env)
    assert by_name.returncode == 0, by_name.stderr
    assert db.get_decision_request(conn, task_id)["repository_id"] == root["id"]

    bad = run_cli("move", str(task_id), "nowhere", env=env)
    assert bad.returncode == 1 and "acme-shop" in bad.stdout and "dashboard" in bad.stdout


def test_pending_all_names_an_item_with_no_folder_and_how_to_fix_it(db_path, globex, tmp_path):
    project, (root, dash) = globex
    conn = db.get_connection()
    env = dict(os.environ, APO_DB=str(db_path))
    task_id = db.create_task(conn, project_id=project["id"], title="Station Review")
    bare = db.get_or_create_project(conn, "bare")
    db.create_task(conn, project_id=bare["id"], title="Somewhere")

    text = run_cli("pending", "--all", env=env).stdout

    globex_block = text.split("Globex")[1].split("\n\n")[0]
    assert "no folder chosen" in globex_block
    assert root["local_path"] in globex_block and dash["local_path"] in globex_block
    assert f"apo move {task_id} <folder>" in globex_block
    assert "bare — no folder registered" in text

    data = json.loads(run_cli("pending", "--all", "--json", env=env).stdout)
    orphan = next(d for d in data if d["id"] == task_id)
    assert orphan["path"] is None and set(orphan["folders"]) == {root["local_path"], dash["local_path"]}


def test_an_agents_goal_step_goes_to_the_folder_it_works_in(globex, monkeypatch, tmp_path):
    from apo import mcp_server

    project, (_, dash) = globex
    outcome_id = db.create_outcome(db.get_connection(), project["id"], "Ship the station")
    monkeypatch.chdir(dash["local_path"])

    step = mcp_server.add_step(outcome_id, "Wire the map", owner="agent")

    assert db.get_decision_request(db.get_connection(), step["id"])["repository_id"] == dash["id"]
    monkeypatch.chdir(tmp_path)
    assert "error" not in mcp_server.add_step(outcome_id, "Pick a vendor", owner="me")
