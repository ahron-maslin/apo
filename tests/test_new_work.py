"""One way to add work to a project: the Work tab's New work form. A task for an
agent, a bug (a task with its report and a label) or something for you, optionally
under a goal, with a due date — and, for an agent's work, started in the background
right away. Starting a session always goes through a tracked item."""
import re
from datetime import date, timedelta

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


@pytest.fixture
def launches(client, monkeypatch):
    from apo import claude_runtime

    calls = []

    def start(path, prompt, resume=None, **kw):
        calls.append({"path": path, "prompt": prompt, **kw})
        return "3f2a9c1b"

    monkeypatch.setattr(claude_runtime, "start_background", start)
    return calls


def _only(conn, project_id):
    [item] = conn.execute("SELECT id FROM decision_requests WHERE project_id = ?", (project_id,)).fetchall()
    return db.get_decision_request(conn, item["id"])


def _in(days):
    return (date.today() + timedelta(days=days)).isoformat()


def test_the_work_tab_has_one_form_for_every_kind_of_work(client, acme):
    html = client.get(f"/projects/{acme['id']}/work").text

    assert len(re.findall(rf'action="/projects/{acme["id"]}/items"', html)) == 1 and "/bugs" not in html
    form = re.search(r'<form[^>]*action="/projects/\d+/items".*?</form>', html, re.S).group(0)
    assert sorted(re.findall(r'name="kind" value="(\w+)"', form)) == ["bug", "me", "task"]
    for field in ("title", "detail", "expected", "steps", "severity", "due_on", "run_now"):
        assert f'name="{field}"' in form, field


def test_a_task_for_an_agent_under_a_goal_with_a_due_date(client, acme):
    conn = db.get_connection()
    goal = db.create_outcome(conn, acme["id"], "Launch the listing")

    resp = client.post(f"/projects/{acme['id']}/items", follow_redirects=False, data={
        "kind": "task", "title": "Add rate limiting", "detail": "100 a minute", "outcome": str(goal), "due_on": _in(3)})

    assert resp.status_code == 303 and resp.headers["location"] == f"/projects/{acme['id']}/work"
    task = _only(conn, acme["id"])
    assert task["kind"] == "task" and task["title"] == "Add rate limiting" and task["question"] == "100 a minute"
    assert task["outcome_id"] == goal and task["due_on"] == _in(3)


def test_the_form_offers_the_projects_active_goals(client, acme):
    conn = db.get_connection()
    db.create_outcome(conn, acme["id"], "Launch the listing")

    html = client.get(f"/projects/{acme['id']}/work").text

    picker = re.search(r'<select[^>]*name="outcome".*?</select>', html, re.S).group(0)
    assert "Launch the listing" in picker


def test_a_bug_keeps_its_report_and_label(client, acme):
    client.post(f"/projects/{acme['id']}/items", data={
        "kind": "bug", "title": "Login does nothing", "detail": "Clicking it on Safari does nothing",
        "expected": "It logs me in", "steps": "Open /login\nClick Log in", "severity": "urgent", "due_on": _in(1)})

    bug = _only(db.get_connection(), acme["id"])
    assert bug["kind"] == "task" and bug["labels"] == ["bug", "urgent"] and bug["due_on"] == _in(1)
    assert bug["summary"] == "Clicking it on Safari does nothing"
    assert "It logs me in" in bug["question"] and "Click Log in" in bug["question"]


def test_a_bug_can_belong_to_a_goal(client, acme):
    goal = db.create_outcome(db.get_connection(), acme["id"], "Launch the listing")

    client.post(f"/projects/{acme['id']}/items", data={"kind": "bug", "title": "Crash on save", "outcome": str(goal)})

    assert _only(db.get_connection(), acme["id"])["outcome_id"] == goal


def test_work_for_you_lands_on_now(client, acme):
    client.post(f"/projects/{acme['id']}/items", data={"kind": "me", "title": "Renew the domain"})

    work = db.project_work(db.get_connection(), acme["id"])
    assert [i["title"] for i in work["needs_you"]] == ["Renew the domain"]


def test_a_title_is_required(client, acme):
    resp = client.post(f"/projects/{acme['id']}/items", data={"kind": "bug", "title": "  "}, follow_redirects=False)

    assert "error=" in resp.headers["location"]
    assert db.get_connection().execute("SELECT COUNT(*) FROM decision_requests").fetchone()[0] == 0


def test_run_now_starts_a_background_session_on_the_new_item(client, acme, launches, tmp_path):
    client.post(f"/projects/{acme['id']}/items", data={"kind": "task", "title": "Add rate limiting", "run_now": "1"})
    client.post(f"/projects/{acme['id']}/items", data={"kind": "bug", "title": "Crash on save", "run_now": "1"})

    conn = db.get_connection()
    ids = [r["id"] for r in conn.execute("SELECT id FROM decision_requests ORDER BY id")]
    assert [(c["path"], c["prompt"]) for c in launches] == [(str(tmp_path), f"/decisions #{i} --here") for i in ids]
    assert all(db.get_decision_request(conn, i)["dispatch_agent_id"] == "3f2a9c1b" for i in ids)


def test_run_now_uses_the_projects_permission_level(client, acme, launches):
    db.set_project_autonomy(db.get_connection(), acme["id"], "edits")

    client.post(f"/projects/{acme['id']}/items", data={"kind": "task", "title": "Add rate limiting", "run_now": "1"})

    assert launches[0]["permission_mode"] == "acceptEdits"


def test_run_now_does_nothing_for_your_own_work(client, acme, launches):
    client.post(f"/projects/{acme['id']}/items", data={"kind": "me", "title": "Renew the domain", "run_now": "1"})

    assert launches == []


def test_a_failed_start_lands_on_the_saved_item_with_the_reason_and_a_retry(client, acme, monkeypatch):
    """The item is saved either way, so the page-level "That didn't go through" would
    read as if nothing was. The item's own page says what failed and can start it again."""
    from apo import claude_runtime

    def fail(*args, **kwargs):
        raise claude_runtime.LaunchError("claude is not installed")

    monkeypatch.setattr(claude_runtime, "start_background", fail)

    resp = client.post(f"/projects/{acme['id']}/items", follow_redirects=False,
                       data={"kind": "task", "title": "Add rate limiting", "run_now": "1"})

    item = _only(db.get_connection(), acme["id"])
    assert item["title"] == "Add rate limiting"
    assert resp.headers["location"].startswith(f"/decisions/{item['id']}?launch_error=")
    html = client.get(resp.headers["location"]).text
    assert "Couldn't start it" in html and "claude is not installed" in html
    assert "That didn't go through" not in html
    assert f'action="/decisions/{item["id"]}/start"' in html


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _shots(*names, data=PNG):
    return [("screenshots", (name, data, "image/png")) for name in names]


def _new_work_form(html):
    return re.search(r'<form[^>]*action="/projects/\d+/items".*?</form>', html, re.S).group(0)


def test_new_work_takes_screenshots(client, acme):
    form = _new_work_form(client.get(f"/projects/{acme['id']}/work").text)

    assert 'enctype="multipart/form-data"' in form
    assert re.search(r'<input[^>]*type="file"[^>]*name="screenshots"', form)


def test_a_bugs_screenshots_are_kept_and_its_file_in_the_repo_shows_them(client, acme, tmp_path):
    client.post(f"/projects/{acme['id']}/items", data={"kind": "bug", "title": "Crash on save"},
                files=_shots("one.png", "two.png"))

    conn = db.get_connection()
    item = _only(conn, acme["id"])
    attachments = db.list_attachments(conn, item["id"])
    assert [a["filename"] for a in attachments] == ["one.png", "two.png"]
    [md] = (tmp_path / ".claude/decisions").glob(f"{item['id']:04d}-*.md")
    for a in attachments:
        assert f"](attachments/{item['id']:04d}/{a['stored_name']})" in md.read_text()
        assert (tmp_path / ".claude/decisions/attachments" / f"{item['id']:04d}" / a["stored_name"]).read_bytes() == PNG


def test_a_session_started_on_it_finds_the_screenshots_already_in_the_repo(client, acme, monkeypatch, tmp_path):
    from apo import claude_runtime

    seen = []

    def start(path, prompt, resume=None, **kw):
        seen.append(len(list((tmp_path / ".claude/decisions/attachments").rglob("*.png"))))
        return "3f2a9c1b"

    monkeypatch.setattr(claude_runtime, "start_background", start)

    client.post(f"/projects/{acme['id']}/items", data={"kind": "bug", "title": "Crash on save", "run_now": "1"},
                files=_shots("one.png"))

    assert seen == [1]


@pytest.mark.parametrize("files", [_shots("notes.png", data=b"just text"), _shots(*[f"{i}.png" for i in range(7)])],
                         ids=["not an image", "more than six"])
def test_screenshots_it_cant_keep_save_nothing(client, acme, files):
    resp = client.post(f"/projects/{acme['id']}/items", data={"kind": "bug", "title": "Crash on save"}, files=files,
                       follow_redirects=False)

    assert "error=" in resp.headers["location"]
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM decision_requests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0


def test_an_empty_file_field_is_not_a_screenshot(client, acme):
    """A browser sends the file field even when nothing was picked: an empty part with no name."""
    client.post(f"/projects/{acme['id']}/items", data={"kind": "task", "title": "Add rate limiting"},
                files=[("screenshots", ("", b"", "application/octet-stream"))])

    conn = db.get_connection()
    assert db.list_attachments(conn, _only(conn, acme["id"])["id"]) == []


def test_the_items_page_shows_its_screenshots(client, acme):
    client.post(f"/projects/{acme['id']}/items", data={"kind": "bug", "title": "Crash on save"},
                files=_shots("one.png"))

    conn = db.get_connection()
    item = _only(conn, acme["id"])
    [attachment] = db.list_attachments(conn, item["id"])
    assert f'src="/attachments/{attachment["id"]}"' in client.get(f"/decisions/{item['id']}").text
