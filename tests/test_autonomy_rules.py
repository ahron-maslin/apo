"""How much a project's background agents may do without asking, and standing rules:
answers you'd give every time, written down once so agents stop asking. Rules live in
each folder's .claude/decisions/POLICY.md and in the project brief."""
import os
import re
from datetime import datetime, timedelta, timezone

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
    (tmp_path / "acme").mkdir()
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path / "acme"))
    return project, str(tmp_path / "acme")


@pytest.fixture
def launches(client, monkeypatch):
    from apo import claude_runtime

    calls = []

    def start(path, prompt, resume=None, **kw):
        calls.append({"path": path, "prompt": prompt, "resume": resume, **kw})
        return "3f2a9c1b"

    monkeypatch.setattr(claude_runtime, "start_background", start)
    return calls


def test_autonomy_defaults_to_asking(acme):
    project, folder = acme
    conn = db.get_connection()

    assert db.project_autonomy(conn, project["id"]) == "ask"
    assert db.permission_mode_for_path(conn, folder) is None
    db.set_project_autonomy(conn, project["id"], "edits")
    assert db.permission_mode_for_path(conn, os.path.join(folder, "src")) == "acceptEdits"
    db.set_project_autonomy(conn, project["id"], "plan")
    assert db.permission_mode_for_path(conn, folder) == "plan"
    with pytest.raises(ValueError):
        db.set_project_autonomy(conn, project["id"], "bypass")


def test_the_runtime_passes_the_mode_to_claude(tmp_path, monkeypatch):
    from apo import claude_runtime

    seen = []
    monkeypatch.setattr(claude_runtime, "_run", lambda args, cwd, timeout: seen.append(args) or "Started 3f2a9c1b")

    claude_runtime.start_background(str(tmp_path), "Fix it", permission_mode="acceptEdits")
    claude_runtime.start_background(str(tmp_path), "Fix it")

    assert seen[0][:3] == ["--bg", "--permission-mode", "acceptEdits"] and "--permission-mode" not in seen[1]
    with pytest.raises(claude_runtime.LaunchError):
        claude_runtime.start_background(str(tmp_path), "Fix it", permission_mode="bypassPermissions")


def test_every_background_start_uses_the_projects_autonomy(client, acme, launches):
    project, folder = acme
    conn = db.get_connection()
    db.set_project_autonomy(conn, project["id"], "edits")
    task_id = db.create_task(conn, project_id=project["id"], title="Add rate limiting")

    client.post(f"/projects/{project['id']}/items", data={"title": "Fix the login", "run_now": "1"})
    client.post(f"/decisions/{task_id}/start")

    assert [c.get("permission_mode") for c in launches] == ["acceptEdits", "acceptEdits"]


def test_a_project_on_ask_starts_without_a_mode(client, acme, launches):
    project, _ = acme

    client.post(f"/projects/{project['id']}/items", data={"title": "Fix the login", "run_now": "1"})

    assert "permission_mode" not in launches[0]


def test_setting_autonomy_from_the_overview(client, acme):
    project, _ = acme

    client.post(f"/projects/{project['id']}/autonomy", data={"autonomy": "plan"})
    html = client.get(f"/projects/{project['id']}").text

    assert db.project_autonomy(db.get_connection(), project["id"]) == "plan"
    assert re.search(r'<option value="plan" selected', html)
    assert "Plan only" in client.get(f"/projects/{project['id']}/work").text


def _policy(folder):
    path = os.path.join(folder, ".claude", "decisions", "POLICY.md")
    return open(path).read() if os.path.exists(path) else None


def test_a_standing_rule_is_written_to_every_folder(acme, tmp_path):
    project, folder = acme
    conn = db.get_connection()
    (tmp_path / "acme" / "web").mkdir()
    db.create_repository(conn, project["id"], "web", str(tmp_path / "acme" / "web"))

    rule_id = db.add_standing_rule(conn, project["id"], "Pick the cheapest reversible option without asking.")

    for path in (folder, str(tmp_path / "acme" / "web")):
        assert "Pick the cheapest reversible option without asking." in _policy(path)
    db.retire_standing_rule(conn, rule_id)
    assert _policy(folder) is None
    assert db.list_standing_rules(conn, project["id"]) == []


def test_agents_get_the_rules_in_the_brief(acme):
    from apo import mcp_server

    project, _ = acme
    db.add_standing_rule(db.get_connection(), project["id"], "Use Postgres for anything new.")

    assert mcp_server.get_project_brief("acme")["standing_rules"] == ["Use Postgres for anything new."]
    assert "POLICY.md" in mcp_server.mcp.instructions


def test_an_answered_decision_can_become_a_rule(client, acme):
    project, folder = acme
    conn = db.get_connection()
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Which queue?", question="?",
                                             requested_by="agent")
    db.answer_decision_request(conn, decision_id, "Postgres", answer_kind="text")

    page = client.get(f"/decisions/{decision_id}").text
    form = re.search(rf'<form[^>]*action="/projects/{project["id"]}/rules".*?</form>', page, re.S).group(0)
    assert "Which queue?" in form and 'name="source_decision_id"' in form

    client.post(f"/projects/{project['id']}/rules", data={"text": "Use Postgres for queues.",
                                                          "source_decision_id": str(decision_id)})

    (rule,) = db.list_standing_rules(conn, project["id"])
    assert rule["text"] == "Use Postgres for queues." and rule["source_decision_id"] == decision_id
    assert "Use Postgres for queues." in client.get(f"/projects/{project['id']}").text


def test_rules_are_added_and_retired_on_the_overview(client, acme):
    project, folder = acme

    client.post(f"/projects/{project['id']}/rules", data={"text": "Never touch the billing tables."})
    (rule,) = db.list_standing_rules(db.get_connection(), project["id"])
    client.post(f"/projects/{project['id']}/rules/{rule['id']}/retire")

    assert db.list_standing_rules(db.get_connection(), project["id"]) == []
    assert client.post(f"/projects/{project['id']}/rules", data={"text": "  "}, follow_redirects=False).status_code == 303


def _answered(conn, project, kind, n):
    for i in range(n):
        decision_id = db.create_decision_request(conn, project_id=project["id"], title=f"{kind} {i}", question="?",
                                                 recommendation="Do the easy thing", requested_by="agent")
        db.answer_decision_request(conn, decision_id, "ok", answer_kind=kind)


def test_accepting_most_recommendations_suggests_a_rule(client, acme):
    project, _ = acme
    conn = db.get_connection()
    _answered(conn, project, "recommendation", 5)
    _answered(conn, project, "text", 1)

    suggestion = db.rule_suggestion(conn, project["id"])

    assert suggestion["accepted"] == 5 and suggestion["answered"] == 6
    import html as html_lib

    html = html_lib.unescape(client.get(f"/projects/{project['id']}").text)
    assert "5 of 6" in html and suggestion["text"] in html


def test_no_suggestion_without_a_clear_pattern_or_once_its_a_rule(acme):
    project, _ = acme
    conn = db.get_connection()
    _answered(conn, project, "recommendation", 3)
    assert db.rule_suggestion(conn, project["id"]) is None

    _answered(conn, project, "recommendation", 3)
    db.add_standing_rule(conn, project["id"], db.rule_suggestion(conn, project["id"])["text"])
    assert db.rule_suggestion(conn, project["id"]) is None
