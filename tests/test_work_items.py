"""Work items beyond decisions and actions: tasks for agents (claimed, done), requests
withdrawn or superseded, and how they project into the repo."""
import pytest

from apo import db


@pytest.fixture
def acme(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo_id = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    return conn, project, repo_id, tmp_path


def test_an_agent_claims_and_completes_a_task(acme):
    conn, project, repo_id, _ = acme
    task = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Add rate limiting")
    db.register_session(conn, "agent-1", None)

    assert db.claim_task(conn, task, "agent-1") is True
    assert db.claim_task(conn, task, "agent-2") is False  # already claimed
    row = db.get_decision_request(conn, task)
    assert row["status"] == "in_progress" and row["claimed_by_session_id"] == "agent-1"

    assert db.complete_task(conn, task, note="Done, 429 after 100 rpm") is True
    assert db.get_decision_request(conn, task)["status"] == "resolved"


def test_a_claim_is_released_when_the_claiming_session_ends(acme):
    conn, project, repo_id, _ = acme
    task = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="T")
    db.register_session(conn, "agent-1", None)
    db.claim_task(conn, task, "agent-1")

    db.session_end(conn, "agent-1", "logout")

    row = db.get_decision_request(conn, task)
    assert row["status"] == "open" and row["claimed_by_session_id"] is None


def test_open_tasks_for_a_project(acme):
    conn, project, repo_id, _ = acme
    ready = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Ready one")
    claimed = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Claimed one")
    done = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Done one")
    db.claim_task(conn, claimed, "s")
    db.claim_task(conn, done, "s")
    db.complete_task(conn, done)

    assert [t["id"] for t in db.list_tasks(conn, project["id"])] == [ready, claimed]


def test_an_agent_can_withdraw_its_request(acme):
    conn, project, repo_id, _ = acme
    db.register_session(conn, "agent-1", None)
    decision = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo_id, title="Q", question="Q?", origin_session_id="agent-1",
    )

    assert db.cancel_request(conn, decision, "Figured it out from the docs") is True

    row = db.get_decision_request(conn, decision)
    assert row["status"] == "cancelled" and row["cancel_reason"] == "Figured it out from the docs"
    assert db.list_sessions(conn)[0]["open_request_count"] == 0
    assert db.cancel_request(conn, decision, "again") is False


def test_a_new_request_can_supersede_an_old_one(acme):
    """Live: "round 2", "(supersedes CLA-95)" and three Amplify rows were unlinked."""
    conn, project, repo_id, _ = acme
    old = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Round 1", question="Q?")
    new = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo_id, title="Round 2", question="Q?", supersedes_id=old,
    )

    old_row = db.get_decision_request(conn, old)
    assert old_row["status"] == "cancelled" and f"#{new}" in old_row["cancel_reason"]
    assert db.get_decision_request(conn, new)["supersedes_id"] == old


def test_only_agent_filed_decisions_and_actions_go_to_linear(acme, monkeypatch):
    conn, project, repo_id, _ = acme
    pushed = []
    monkeypatch.setattr(db, "_push_to_sources", lambda conn, decision_id: pushed.append(decision_id))

    decision = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    task = db.create_task(conn, project_id=project["id"], title="T")
    outcome_id = db.create_outcome(conn, project["id"], "O")
    my_step = db.add_step(conn, outcome_id, "Mine", owner="me")

    assert pushed == [decision]
    assert task not in pushed and my_step not in pushed


def test_the_repo_projection_shows_tasks_ready_for_an_agent(acme):
    conn, project, repo_id, tmp_path = acme
    task = db.create_task(conn, project_id=project["id"], repository_id=repo_id, title="Add rate limiting",
                          detail="100 rpm per key")
    wait = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="App review",
                                      question="Waiting on Apple", kind="wait", requested_by="human")

    index = (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()
    assert f"| READY | {task:04d} |" in index
    assert f"| WAITING | {wait:04d} |" in index
    record = (tmp_path / ".claude" / "decisions" / f"{task:04d}-add-rate-limiting.md").read_text()
    assert "claim_task" in record and "complete_task" in record

    db.claim_task(conn, task, "s")
    assert f"| IN PROGRESS | {task:04d} |" in (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()


def test_a_task_waiting_on_another_step_is_not_ready_until_that_one_is_done(acme):
    """/decisions and the session-start nudge both go by READY rows, so a blocked task
    shown as READY gets worked before the step it waits on."""
    conn, project, repo_id, tmp_path = acme
    outcome_id = db.create_outcome(conn, project["id"], "Rename")
    approve = db.add_step(conn, outcome_id, "Approve the venv change", owner="me", repository_id=repo_id)
    move = db.add_step(conn, outcome_id, "Move the folder", owner="agent", blocked_by=[approve], repository_id=repo_id)

    index = (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()
    assert f"| WAITING | {move:04d} |" in index
    assert f"| READY | {move:04d} |" not in index

    db.complete_step(conn, approve, by="human")
    assert f"| READY | {move:04d} |" in (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()
