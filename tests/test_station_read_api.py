"""Read-only JSON for a local dashboard polling apo: each item's blockers and claim
state on /api/decisions, and a project's outcomes with their steps on /api/outcomes."""
import pytest
from fastapi.testclient import TestClient

from apo import db
from apo.web import app as app_module


@pytest.fixture
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return conn, project, TestClient(app_module.app)


def _row(client, item_id, **params):
    return next(r for r in client.get("/api/decisions", params=params).json() if r["id"] == item_id)


def test_decision_rows_carry_outcome_blockers_and_claim_state(acme):
    conn, project, client = acme
    outcome = db.create_outcome(conn, project["id"], "Launch")
    first = db.add_step(conn, outcome, "Build", owner="agent")
    second = db.add_step(conn, outcome, "Deploy", owner="agent", blocked_by=[first])

    row = _row(client, second, project="acme")
    assert row["outcome_id"] == outcome
    assert row["blocked_by"] == [first]
    assert row["claimed_by_session_id"] is None
    assert row["claim_stale"] is False
    assert "dispatched_at" in row and row["dispatched_at"] is None

    db.complete_task(conn, first, note="built")
    assert _row(client, second, project="acme")["blocked_by"] == []


def test_a_claim_whose_session_ended_is_stale(acme):
    conn, project, client = acme
    task = db.create_task(conn, project_id=project["id"], title="Rate limit")
    db.claim_task(conn, task, "s1")
    db.register_session(conn, "s1", None)

    row = _row(client, task, project="acme")
    assert row["claimed_by_session_id"] == "s1" and row["claim_stale"] is False

    conn.execute("UPDATE sessions SET status = 'ended' WHERE claude_session_id = 's1'")
    conn.commit()
    assert _row(client, task, project="acme")["claim_stale"] is True
    assert _row(client, task)["claim_stale"] is True


def test_existing_decision_fields_are_unchanged(acme):
    _, _, client = acme
    decision_id = client.post("/api/decisions", json={"project": "acme", "title": "T", "question": "Q?"}).json()["id"]
    row = _row(client, decision_id, project="acme")
    for field in ("title", "question", "status", "kind", "project_name", "created_at", "options"):
        assert field in row
    assert row["outcome_id"] is None and row["blocked_by"] == [] and row["claim_stale"] is False


def test_mcp_list_tasks_reports_the_same_state(acme, monkeypatch):
    from apo import mcp_server

    conn, project, client = acme
    first = db.create_task(conn, project_id=project["id"], title="A")
    second = db.create_task(conn, project_id=project["id"], title="B", blocked_by=[first])
    db.claim_task(conn, first, "s1")
    db.register_session(conn, "s1", None)
    conn.execute("UPDATE sessions SET status = 'ended' WHERE claude_session_id = 's1'")
    conn.commit()

    listed = {t["id"]: t for t in mcp_server.list_tasks(project="acme")}
    for item_id in (first, second):
        row = _row(client, item_id, project="acme")
        assert listed[item_id]["blocked_by"] == row["blocked_by"]
        assert listed[item_id]["claim_stale"] == row["claim_stale"]
    assert listed[first]["claim_stale"] is True and listed[second]["blocked_by"] == [first]


def test_outcomes_list_steps_with_owner_kind_status_and_counts(acme):
    conn, project, client = acme
    outcome = db.create_outcome(conn, project["id"], "Launch the site", done_when="Live with a real payment")
    build = db.add_step(conn, outcome, "Configure the build", owner="agent")
    dns = db.add_step(conn, outcome, "Add the DNS record", owner="me", blocked_by=[build])
    wait = db.add_step(conn, outcome, "DNS propagation", owner="external", waiting_on="Cloudflare")
    dropped = db.add_step(conn, outcome, "Old idea", owner="agent")
    db.cancel_request(conn, dropped, "not needed")
    db.complete_step(conn, build, by="agent")

    response = client.get("/api/outcomes", params={"project": "acme"})
    assert response.status_code == 200
    [goal] = response.json()
    assert goal["id"] == outcome and goal["title"] == "Launch the site"
    assert goal["project"] == "acme" and goal["status"] == "active"
    assert goal["done_when"] == "Live with a real payment"
    assert goal["created_at"] and goal["updated_at"]
    assert goal["done"] == 1 and goal["total"] == 3
    assert [(s["id"], s["owner"], s["kind"]) for s in goal["steps"]] == [
        (build, "agent", "task"), (dns, "me", "action"), (wait, "external", "wait"), (dropped, "agent", "task")]
    steps = {s["id"]: s for s in goal["steps"]}
    assert steps[build]["status"] == "resolved" and steps[build]["done"] is True
    assert steps[dns]["status"] == "open" and steps[dns]["blocked_by"] == []
    assert steps[dropped]["status"] == "cancelled" and steps[dropped]["done"] is False
    assert steps[dns]["title"] == "Add the DNS record"


def test_outcomes_are_filtered_by_project_and_status(acme):
    conn, project, client = acme
    other = db.get_or_create_project(conn, "other")
    mine = db.create_outcome(conn, project["id"], "Mine")
    db.create_outcome(conn, other["id"], "Theirs")
    achieved = db.create_outcome(conn, project["id"], "Shipped")
    db.set_outcome_status(conn, achieved, "achieved")

    assert [g["id"] for g in client.get("/api/outcomes", params={"project": "acme"}).json()] == [mine]
    everything = client.get("/api/outcomes", params={"project": "acme", "status": "all"}).json()
    assert [g["id"] for g in everything] == [mine, achieved]
    assert everything[1]["status"] == "achieved"
    only = client.get("/api/outcomes", params={"project": "acme", "status": "achieved"}).json()
    assert [g["id"] for g in only] == [achieved]
    assert {g["title"] for g in client.get("/api/outcomes").json()} == {"Mine", "Theirs"}
    assert client.get("/api/outcomes", params={"project": "nope"}).json() == []


def test_outcomes_reject_an_unknown_status(acme):
    _, _, client = acme
    response = client.get("/api/outcomes", params={"status": "bogus"})
    assert response.status_code == 400
    assert "detail" in response.json()
