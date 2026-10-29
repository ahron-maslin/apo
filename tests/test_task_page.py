"""A task's page shows it as work for an agent, not a question for you: no answer box,
a timeline of ready → claimed → done, and ways to start it or close it yourself. A
wait shows who it waits on. Starting a session on a project with several folders runs
it in the folder you pick."""
import re

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


@pytest.fixture
def project(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "Globex")
    repos = []
    for name in ("acme-shop", "dashboard"):
        (tmp_path / name).mkdir()
        repos.append(db.create_repository(conn, project["id"], name, str(tmp_path / name)))
    return project, repos


@pytest.fixture
def launches(client, monkeypatch):
    from apo import claude_runtime

    calls = []

    def start(path, prompt, resume=None):
        calls.append((path, prompt))
        return "3f2a9c1b"

    monkeypatch.setattr(claude_runtime, "start_background", start)
    return calls


def _main(html):
    return re.search(r'<article class="detail-main".*?</article>', html, re.S).group(0)


def _aside(html):
    return re.search(r'<aside class="detail-aside".*?</aside>', html, re.S).group(0)


def _current_step(html):
    return re.search(r'<li class="is-current">.*?step-title">(.*?)</div>', _aside(html), re.S).group(1)


def test_a_ready_task_is_work_for_an_agent_not_a_question_for_you(client, project):
    p, (_, dash) = project
    task_id = db.create_task(db.get_connection(), project_id=p["id"], title="Station Review",
                             detail="Audit the Ops Station code", repository_id=dash["id"])

    html = client.get(f"/decisions/{task_id}").text

    assert "Your answer" not in html and "Waiting on you" not in html and "Answer below" not in html
    assert "<dt>Type</dt><dd>Task</dd>" in html
    assert "Added by you" in _aside(html) and _current_step(html) == "Ready for an agent"
    assert f'{dash["local_path"]}/.claude/decisions/INDEX.md' in _main(html)
    assert f'action="/decisions/{task_id}/start"' in html and f'action="/items/{task_id}/done"' in html


def test_the_brief_is_shown_once(client, project):
    p, (root, _) = project
    task_id = db.create_task(db.get_connection(), project_id=p["id"], title="Station Review",
                             detail="Audit the Ops Station code", repository_id=root["id"])

    assert _main(client.get(f"/decisions/{task_id}").text).count("Audit the Ops Station code") == 1


def test_a_claimed_task_shows_the_agent_has_it(client, project):
    p, (root, _) = project
    conn = db.get_connection()
    task_id = db.create_task(conn, project_id=p["id"], title="Station Review", repository_id=root["id"])
    db.claim_task(conn, task_id, "sess-1")

    html = client.get(f"/decisions/{task_id}").text

    assert _current_step(html) == "Claimed by an agent"
    assert f'action="/decisions/{task_id}/start"' not in html
    assert f'action="/items/{task_id}/done"' in html


def test_a_done_task_shows_what_the_agent_said(client, project):
    p, (root, _) = project
    conn = db.get_connection()
    task_id = db.create_task(conn, project_id=p["id"], title="Station Review", repository_id=root["id"])
    db.claim_task(conn, task_id, "sess-1")
    db.complete_task(conn, task_id, note="Wrote the review to docs/review.md")

    html = client.get(f"/decisions/{task_id}").text

    assert "Wrote the review to docs/review.md" in _main(html)
    assert 'class="is-current"' not in _aside(html).split("<h2>Progress</h2>")[1].split("</ol>")[0]
    assert f'action="/items/{task_id}/done"' not in html and "Your answer" not in html


def test_a_wait_shows_who_it_waits_on(client, project):
    p, _ = project
    conn = db.get_connection()
    outcome_id = db.create_outcome(conn, p["id"], "Ship the station")
    wait_id = db.add_step(conn, outcome_id, "App review", owner="external", waiting_on="Apple review")

    html = client.get(f"/decisions/{wait_id}").text

    assert _current_step(html) == "Waiting on Apple review"
    assert "<dt>Type</dt><dd>Wait</dd>" in html and "Your answer" not in html
    assert f'action="/items/{wait_id}/done"' in html


def test_your_own_items_say_you_added_them(client, project):
    p, (root, _) = project
    item = db.create_decision_request(db.get_connection(), project_id=p["id"], title="Renew the domain",
                                      question="Renew the domain", kind="action", requested_by="human",
                                      repository_id=root["id"])

    assert "Added by you" in _aside(client.get(f"/decisions/{item}").text)


def test_starting_a_ready_task_in_the_background(client, project, launches):
    p, (_, dash) = project
    conn = db.get_connection()
    task_id = db.create_task(conn, project_id=p["id"], title="Station Review", repository_id=dash["id"])

    resp = client.post(f"/decisions/{task_id}/start", follow_redirects=False)

    assert resp.status_code == 303 and resp.headers["location"] == f"/decisions/{task_id}"
    assert launches == [(dash["local_path"], f"/decisions #{task_id} --here")]
    assert db.get_decision_request(conn, task_id)["dispatch_agent_id"] == "3f2a9c1b"
    assert "claude attach 3f2a9c1b" in client.get(f"/decisions/{task_id}").text


def test_a_task_without_a_folder_cant_be_started(client, project, launches):
    p, _ = project
    task_id = db.create_task(db.get_connection(), project_id=p["id"], title="Station Review")

    resp = client.post(f"/decisions/{task_id}/start", follow_redirects=False)

    assert "launch_error=" in resp.headers["location"] and launches == []


def test_starting_work_now_runs_in_the_folder_you_pick(client, project, launches):
    p, (root, dash) = project

    html = client.get(f"/projects/{p['id']}/work").text
    form = re.search(rf'<form[^>]*action="/projects/{p["id"]}/items".*?</form>', html, re.S).group(0)
    assert 'name="repository"' in form and 'name="run_now"' in form

    client.post(f"/projects/{p['id']}/items", data={"title": "Fix the map", "run_now": "1", "repository": str(dash["id"])})
    client.post(f"/projects/{p['id']}/items", data={"title": "Fix the login", "run_now": "1"})

    ids = [r["id"] for r in db.get_connection().execute("SELECT id FROM decision_requests ORDER BY id")]
    assert launches == [(dash["local_path"], f"/decisions #{ids[0]} --here"), (root["local_path"], f"/decisions #{ids[1]} --here")]


def test_a_task_with_no_detail_doesnt_repeat_its_title(client, project):
    p, (root, _) = project
    task_id = db.create_task(db.get_connection(), project_id=p["id"], title="Turn off the SSO wall",
                             repository_id=root["id"])

    assert _main(client.get(f"/decisions/{task_id}").text).count("Turn off the SSO wall") == 1
