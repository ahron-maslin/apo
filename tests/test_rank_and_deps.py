"""Hand-set order inside the work list, and blocked-by links edited from the item page."""
import re

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo_id = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return conn, project, repo_id, tmp_path


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _tasks(conn, project, repo_id, n):
    return [db.create_task(conn, project_id=project["id"], repository_id=repo_id, title=f"T{i}") for i in range(n)]


def _ready(conn, project):
    return [i["id"] for i in db.project_work(conn, project["id"])["ready"]]


def _index(tmp_path):
    return (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()


def _index_ready_ids(tmp_path):
    return [int(m) for m in re.findall(r"^\| READY \| (\d+)", _index(tmp_path), re.M)]


def test_with_no_ranks_the_order_is_unchanged(acme):
    conn, project, repo_id, _ = acme
    a, b, c = _tasks(conn, project, repo_id, 3)
    assert _ready(conn, project) == [a, b, c]
    assert [t["id"] for t in db.list_tasks(conn, project["id"])] == [a, b, c]


def test_do_this_next_puts_it_first_everywhere(acme):
    conn, project, repo_id, tmp_path = acme
    a, b, c = _tasks(conn, project, repo_id, 3)
    db.move_rank(conn, c, "top")
    assert _ready(conn, project) == [c, a, b]
    assert [t["id"] for t in db.list_tasks(conn, project["id"])] == [c, a, b]
    assert _index_ready_ids(tmp_path) == [c, a, b]


def test_move_up_and_down_swap_neighbours_in_the_group(acme):
    conn, project, repo_id, _ = acme
    a, b, c = _tasks(conn, project, repo_id, 3)
    db.move_rank(conn, c, "up")
    assert _ready(conn, project) == [a, c, b]
    db.move_rank(conn, a, "down")
    assert _ready(conn, project) == [c, a, b]
    db.move_rank(conn, c, "up")  # already first: no change, no error
    assert _ready(conn, project) == [c, a, b]


def test_unknown_direction_or_item_is_rejected(acme):
    conn, project, repo_id, _ = acme
    (a,) = _tasks(conn, project, repo_id, 1)
    with pytest.raises(ValueError):
        db.move_rank(conn, a, "sideways")
    with pytest.raises(ValueError):
        db.move_rank(conn, 999, "up")


def test_rank_route_works_as_a_form_post(client, acme):
    conn, project, repo_id, _ = acme
    a, b = _tasks(conn, project, repo_id, 2)
    r = client.post(f"/items/{b}/rank", data={"direction": "top", "next": f"/projects/{project['id']}/work"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/projects/{project['id']}/work")
    assert _ready(conn, project) == [b, a]
    assert client.post("/items/999/rank", data={"direction": "up"}).status_code == 404
    assert client.post(f"/items/{a}/rank", data={"direction": "bad"}).status_code == 400


def test_work_page_shows_rank_controls(client, acme):
    conn, project, repo_id, _ = acme
    _tasks(conn, project, repo_id, 2)
    html = client.get(f"/projects/{project['id']}/work").text
    assert 'name="direction" value="top"' in html and 'value="down"' in html


def test_dependency_detail_lists_blockers_and_dependents(acme):
    conn, project, repo_id, _ = acme
    a, b = _tasks(conn, project, repo_id, 2)
    db.add_dependency(conn, b, a)
    assert [x["id"] for x in db.item_dependencies(conn, b)["blockers"]] == [a]
    assert [x["id"] for x in db.item_dependencies(conn, a)["dependents"]] == [b]


def test_remove_dependency_makes_the_item_ready_again(acme):
    conn, project, repo_id, _ = acme
    a, b = _tasks(conn, project, repo_id, 2)
    db.add_dependency(conn, b, a)
    assert _ready(conn, project) == [a]
    db.remove_dependency(conn, b, a)
    assert db.open_blockers(conn, b) == []
    assert _ready(conn, project) == [a, b]


def test_candidates_exclude_self_existing_cycles_and_done(acme):
    conn, project, repo_id, _ = acme
    a, b, c, d = _tasks(conn, project, repo_id, 4)
    db.add_dependency(conn, b, a)
    db.add_dependency(conn, c, b)
    db.cancel_request(conn, d, "no")
    assert [x["id"] for x in db.dependency_candidates(conn, a)] == []
    assert [x["id"] for x in db.dependency_candidates(conn, c)] == [a]


def test_dependency_routes(client, acme):
    conn, project, repo_id, _ = acme
    a, b = _tasks(conn, project, repo_id, 2)
    r = client.post(f"/decisions/{b}/blockers", data={"blocker": str(a)}, follow_redirects=False)
    assert r.status_code == 303
    assert db.open_blockers(conn, b) == [a]
    page = client.get(f"/decisions/{b}").text
    assert "Blocked by" in page and f"#{a}" in page
    assert "Blocks" in client.get(f"/decisions/{a}").text
    r = client.post(f"/decisions/{a}/blockers", data={"blocker": str(b)}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = client.post(f"/decisions/{a}/blockers", data={"blocker": str(a)}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    client.post(f"/decisions/{b}/blockers/{a}/remove")
    assert db.open_blockers(conn, b) == []


def test_index_shows_waiting_after_link_and_ready_after_removal(acme):
    conn, project, repo_id, tmp_path = acme
    a, b = _tasks(conn, project, repo_id, 2)
    db.link_blocker(conn, b, a)
    assert "| WAITING |" in _index(tmp_path)
    db.remove_dependency(conn, b, a)
    assert "| WAITING |" not in _index(tmp_path)


def test_mcp_set_task_blockers(acme):
    from apo import mcp_server

    conn, project, repo_id, _ = acme
    a, b = _tasks(conn, project, repo_id, 2)
    assert mcp_server.set_task_blockers(b, add=[a])["blocked_by"] == [a]
    assert mcp_server.set_task_blockers(b, remove=[a])["blocked_by"] == []
    assert "error" in mcp_server.set_task_blockers(a, add=[a])
