"""Reporting a bug to a project: it becomes work for that project's agents — a READY
task in the repo's INDEX.md, so the next session (or /decisions) picks it up — and,
if you want, a background session starts on it right away."""
import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apo import db

PALETTE = str(Path(__file__).resolve().parent.parent / "src" / "apo" / "web" / "static" / "palette.js")


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


def test_a_bug_is_a_task_for_the_projects_agents(acme, tmp_path):
    conn = db.get_connection()

    bug_id = db.report_bug(conn, acme["id"], "Login button does nothing",
                           happened="Clicking it on Safari does nothing", expected="It logs me in",
                           steps="Open /login in Safari\nClick Log in", severity="urgent")

    bug = db.get_decision_request(conn, bug_id)
    assert bug["kind"] == "task" and bug["assignee"] == "agent" and bug["requested_by"] == "human"
    assert bug["labels"] == ["bug", "urgent"]
    assert "Clicking it on Safari does nothing" in bug["question"] and "It logs me in" in bug["question"]
    assert bug["summary"] == "Clicking it on Safari does nothing"
    index = (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()
    assert "READY" in index and "Login button does nothing" in index


def test_bugs_list_on_the_projects_work_tab(client, acme):
    db.report_bug(db.get_connection(), acme["id"], "Login button does nothing", happened="Nothing happens")

    text = client.get(f"/projects/{acme['id']}/work").text

    assert "Login button does nothing" in text
    assert f'action="/projects/{acme["id"]}/items"' in text and 'name="kind" value="bug"' in text


def test_reporting_from_the_page(client, acme):
    resp = client.post(f"/projects/{acme['id']}/items", data={"kind": "bug", "title": "Crash on save", "detail": "500 error"},
                       follow_redirects=False)

    assert resp.status_code == 303
    assert [t["title"] for t in db.list_tasks(db.get_connection(), acme["id"])] == ["Crash on save"]


def test_a_bug_needs_a_title(client, acme):
    resp = client.post(f"/projects/{acme['id']}/items", data={"kind": "bug", "title": "  "})

    assert "Say what needs doing first." in resp.text
    assert db.list_tasks(db.get_connection(), acme["id"]) == []


def test_fix_it_now_starts_a_background_session_on_it(client, acme, tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    fake = tmp_path / "fake-claude"
    fake.write_text(f'#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" >> {log}\necho "Started 3f2a9c1b"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("APO_CLAUDE_BIN", str(fake))

    client.post(f"/projects/{acme['id']}/items", data={"kind": "bug", "title": "Crash on save", "run_now": "1"})

    bug_id = db.list_tasks(db.get_connection(), acme["id"])[0]["id"]
    assert log.read_text().strip() == f"{tmp_path}|--bg /decisions #{bug_id} --here"


def test_bugs_are_not_your_move(acme):
    conn = db.get_connection()
    db.report_bug(conn, acme["id"], "Crash on save")

    assert db.now_queue(conn)["your_move"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_palette_bug_verb_reports_to_a_project():
    data = {"pages": [], "projects": [{"id": 3, "name": "acme"}], "requests": [], "sessions": []}
    script = (f"const P = require({json.dumps(PALETTE)}); "
              f"console.log(JSON.stringify(P.parseCommand('bug acme Login button does nothing', {json.dumps(data)})));")
    command = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)

    assert command["run"] == {"method": "post", "action": "/projects/3/items",
                              "fields": {"kind": "bug", "title": "Login button does nothing"}}
