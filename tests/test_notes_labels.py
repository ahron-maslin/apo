"""A notes thread on each item, and labels with an owner/status/label filter on Work and Search."""
import os

import pytest
from fastapi.testclient import TestClient

from apo import db
from apo import mcp_server

from test_cli import run_cli


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo_id = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return conn, project, repo_id, tmp_path


def _task(acme, title="Add rate limiting"):
    conn, project, repo_id, _ = acme
    return db.create_task(conn, project_id=project["id"], repository_id=repo_id, title=title)


def test_notes_are_listed_oldest_first_with_their_author(acme):
    conn = acme[0]
    item = _task(acme)
    db.add_note(conn, item, "first", author="human")
    db.add_note(conn, item, "second", author="session-9")

    notes = db.list_notes(conn, item)

    assert [(n["author"], n["body"]) for n in notes] == [("human", "first"), ("session-9", "second")]
    assert db.get_decision_request(conn, item)["note_count"] == 2


def test_a_blank_note_or_unknown_item_is_refused(acme):
    conn = acme[0]
    item = _task(acme)
    with pytest.raises(ValueError):
        db.add_note(conn, item, "   ")
    with pytest.raises(ValueError):
        db.add_note(conn, 9999, "hello")


def test_a_note_writes_an_event(acme):
    conn = acme[0]
    item = _task(acme)
    db.add_note(conn, item, "watch the Redis limit", author="human")

    verbs = [e["verb"] for e in db.list_events(conn, decision_id=item, min_importance=0)]
    assert "item.noted" in verbs


def test_notes_are_a_trailing_section_in_the_items_markdown(acme):
    conn, _, _, tmp_path = acme
    item = _task(acme)
    path = next((tmp_path / ".claude" / "decisions").glob(f"{item:04d}-*.md"))
    before = path.read_text()
    assert "## Notes" not in before

    db.add_note(conn, item, "use the existing limiter\nnot a new one", author="human")

    after = path.read_text()
    assert after.startswith(before.rstrip("\n"))
    tail = after[after.index("## Notes"):]
    assert "human" in tail and "use the existing limiter" in tail and "not a new one" in tail
    assert after.rstrip().endswith("not a new one")


def test_the_item_page_takes_a_note_without_javascript(acme, client):
    conn = acme[0]
    item = _task(acme)

    response = client.post(f"/decisions/{item}/notes", data={"body": "remember the 429 header"}, follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"].startswith(f"/decisions/{item}")
    assert db.list_notes(conn, item)[0]["author"] == "human"
    page = client.get(f"/decisions/{item}").text
    assert "remember the 429 header" in page


def test_a_blank_note_from_the_page_adds_nothing(acme, client):
    conn = acme[0]
    item = _task(acme)
    response = client.post(f"/decisions/{item}/notes", data={"body": " "}, follow_redirects=False)
    assert response.status_code == 303
    assert db.list_notes(conn, item) == []


def test_work_rows_show_the_note_count(acme, client):
    conn, project = acme[0], acme[1]
    item = _task(acme)
    db.add_note(conn, item, "a")
    db.add_note(conn, item, "b")

    assert "2 notes" in client.get(f"/projects/{project['id']}/work").text


def test_now_rows_show_the_note_count(acme, client):
    conn, project, repo_id, _ = acme
    ask = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Q", question="Q?")
    db.add_note(conn, ask, "context from me")

    assert "1 note" in client.get("/").text


def test_mcp_add_note_uses_the_calling_session(acme, monkeypatch):
    conn = acme[0]
    item = _task(acme)
    monkeypatch.setattr(mcp_server.claude_runtime, "current_session_id", lambda pid=None: "live-session")

    assert mcp_server.add_note(item, "found the cause")["added"] is True
    assert db.list_notes(conn, item)[0]["author"] == "live-session"
    assert mcp_server.add_note(9999, "x")["added"] is False


def test_cli_note(acme, db_path):
    conn = acme[0]
    item = _task(acme)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["ADO_DB"] = str(db_path)

    result = run_cli("note", str(item), "ping the vendor", env=env)

    assert result.returncode == 0, result.stderr
    assert db.list_notes(conn, item)[0]["body"] == "ping the vendor"
    assert run_cli("note", "9999", "x", env=env).returncode == 1


def test_labels_add_and_remove_on_the_item_page(acme, client):
    conn = acme[0]
    item = _task(acme)

    client.post(f"/decisions/{item}/labels", data={"label": "Backend"})
    client.post(f"/decisions/{item}/labels", data={"label": "backend"})
    client.post(f"/decisions/{item}/labels", data={"label": "urgent"})
    assert db.get_decision_request(conn, item)["labels"] == ["backend", "urgent"]
    assert "?label=backend" in client.get(f"/decisions/{item}").text

    client.post(f"/decisions/{item}/labels/remove", data={"label": "backend"})
    assert db.get_decision_request(conn, item)["labels"] == ["urgent"]


def test_a_bug_keeps_its_existing_labels(acme):
    conn, project, repo_id, _ = acme
    bug = db.add_work(conn, project["id"], "bug", "Crash", severity="urgent", repository_id=repo_id)
    db.add_label(conn, bug, "api")
    assert db.get_decision_request(conn, bug)["labels"] == ["bug", "urgent", "api"]


def test_new_work_takes_labels(acme, client):
    conn, project = acme[0], acme[1]
    client.post(f"/projects/{project['id']}/items", data={"kind": "task", "title": "T", "labels": "api, Infra,api"})
    item = db.project_work(conn, project["id"])["ready"][0]
    assert item["labels"] == ["api", "infra"]


def test_work_filters_by_label_owner_and_status(acme, client):
    conn, project, repo_id, _ = acme
    a = _task(acme, "Agent api task")
    _task(acme, "Agent web task")
    mine = db.add_work(conn, project["id"], "me", "My api chore", repository_id=repo_id)
    db.add_label(conn, a, "api")
    db.add_label(conn, mine, "api")
    base = f"/projects/{project['id']}/work"

    page = client.get(base).text
    assert all(t in page for t in ("Agent api task", "Agent web task", "My api chore"))

    page = client.get(base + "?label=api").text
    assert "Agent api task" in page and "My api chore" in page and "Agent web task" not in page

    page = client.get(base + "?label=api&owner=agent").text
    assert "Agent api task" in page and "My api chore" not in page

    page = client.get(base + "?status=needs_you").text
    assert "My api chore" in page and "Agent api task" not in page
    assert 'name="label"' in page


def test_all_projects_work_filters_too(acme, client):
    a = _task(acme, "Labelled one")
    _task(acme, "Plain one")
    db.add_label(acme[0], a, "api")
    page = client.get("/projects/work?label=api").text
    assert "Labelled one" in page and "Plain one" not in page


def test_search_filters_by_label_owner_and_status(acme, client):
    conn, project, repo_id, _ = acme
    a = _task(acme, "Stripe webhook retry")
    _task(acme, "Stripe refund flow")
    db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Stripe keys?", question="Which?")
    db.add_label(conn, a, "billing")

    titles = lambda **kw: sorted(h["title"] for h in db.search(conn, "stripe", **kw))
    assert len(titles()) == 3
    assert titles(label="billing") == ["Stripe webhook retry"]
    assert titles(owner="me") == ["Stripe keys?"]
    assert titles(owner="agent", status="open") == ["Stripe refund flow", "Stripe webhook retry"]
    assert titles(status="resolved") == []

    page = client.get("/search?q=stripe&label=billing").text
    assert "webhook retry" in page and "refund flow" not in page
    assert 'name="label"' in page
