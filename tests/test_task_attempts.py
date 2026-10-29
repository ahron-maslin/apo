"""Retry, attempt history and structured completion for agent tasks. All additive:
claim_task and complete_task behave as before when called as before."""
import os

import pytest
from fastapi.testclient import TestClient

from apo import db
from test_cli import run_cli


@pytest.fixture
def setup(db_path, tmp_path):
    (tmp_path / "repo").mkdir()
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "repo", str(tmp_path / "repo"))
    task = db.create_task(conn, project_id=project["id"], title="Add rate limiting", detail="100 rpm")
    return conn, project, task


def _decisions_dir(tmp_path):
    return tmp_path / "repo" / ".claude" / "decisions"


def test_attempts_record_claim_and_complete(setup):
    conn, _, task = setup
    assert db.claim_task(conn, task, "s1")
    assert db.complete_task(conn, task, note="shipped")
    attempts = db.list_attempts(conn, task)
    assert [a["event"] for a in attempts] == ["claimed", "completed"]
    assert attempts[0]["session_id"] == "s1"
    assert attempts[1]["note"] == "shipped" and attempts[1]["outcome"] == "done"


def test_session_end_release_is_recorded(setup):
    conn, _, task = setup
    db.claim_task(conn, task, "s1")
    db.session_end(conn, "s1")
    assert db.get_decision_request(conn, task)["status"] == "open"
    assert [a["event"] for a in db.list_attempts(conn, task)] == ["claimed", "released"]


def test_retry_puts_stuck_and_cancelled_back_to_ready(setup):
    conn, _, task = setup
    db.claim_task(conn, task, "s1")
    assert db.retry_task(conn, task, note="agent died", by="human")
    t = db.get_decision_request(conn, task)
    assert t["status"] == "open" and t["claimed_by_session_id"] is None and t["claimed_at"] is None
    assert db.claim_task(conn, task, "s2")
    db.cancel_request(conn, task, "gave up")
    assert db.retry_task(conn, task)
    assert db.get_decision_request(conn, task)["status"] == "open"
    assert [a["event"] for a in db.list_attempts(conn, task)] == ["claimed", "retried", "claimed", "retried"]


def test_retry_reopens_a_finished_task_and_keeps_its_note(setup):
    conn, _, task = setup
    db.complete_task(conn, task, note="half done", outcome="partial", not_done="the tests")
    assert db.retry_task(conn, task)
    t = db.get_decision_request(conn, task)
    assert t["status"] == "open" and t["resolved_at"] is None
    assert db.list_attempts(conn, task)[0]["note"] == "half done"


def test_retry_refuses_an_open_task_and_other_kinds(setup):
    conn, project, task = setup
    assert not db.retry_task(conn, task)
    d = db.create_decision_request(conn, project_id=project["id"], title="q", question="q?")
    assert not db.retry_task(conn, d)


def test_stuck_claims_lists_tasks_whose_session_ended(setup):
    conn, project, task = setup
    db.claim_task(conn, task, "s1")
    assert db.stuck_claims(conn, project["id"]) == []
    db.register_session(conn, "s1", None)
    assert db.stuck_claims(conn, project["id"]) == []
    conn.execute("UPDATE sessions SET status = 'ended' WHERE claude_session_id = 's1'")
    conn.commit()
    assert [t["id"] for t in db.stuck_claims(conn, project["id"])] == [task]


def test_structured_completion_is_stored(setup):
    conn, _, task = setup
    assert db.complete_task(conn, task, note="n", outcome="partial", done="API", not_done="docs",
                            left_for_human="merge", branch="feat/x")
    c = db.get_decision_request(conn, task)["completion"]
    assert c == {"outcome": "partial", "done": "API", "not_done": "docs", "left_for_human": "merge", "branch": "feat/x"}


def test_plain_completion_has_no_structured_data(setup):
    conn, _, task = setup
    db.complete_task(conn, task, note="n")
    assert db.get_decision_request(conn, task)["completion"] is None


def test_bad_outcome_is_rejected_and_changes_nothing(setup):
    conn, _, task = setup
    with pytest.raises(ValueError):
        db.complete_task(conn, task, outcome="great")
    assert db.get_decision_request(conn, task)["status"] == "open"


def test_partial_shows_as_partly_done_on_work(setup):
    conn, project, task = setup
    db.complete_task(conn, task, outcome="partial", not_done="docs")
    groups = db.project_work(conn, project["id"])
    assert [i["state"] for i in groups["done"] if i["id"] == task] == ["Partly done"]


def test_repo_markdown_includes_completion_without_losing_layout(setup, tmp_path):
    conn, _, task = setup
    db.complete_task(conn, task, note="n", outcome="partial", done="API", not_done="docs", branch="feat/x")
    text = next(_decisions_dir(tmp_path).glob(f"{task:04d}-*.md")).read_text()
    assert "## Answer" in text and "## Completion" in text
    assert "**Not done:** docs" in text and "**Outcome:** partial" in text
    assert "| DONE |" in (_decisions_dir(tmp_path) / "INDEX.md").read_text()


def test_retried_task_is_ready_in_index(setup, tmp_path):
    conn, _, task = setup
    db.claim_task(conn, task, "s1")
    db.retry_task(conn, task)
    assert "| READY |" in (_decisions_dir(tmp_path) / "INDEX.md").read_text()


def test_mcp_tools(setup, monkeypatch):
    from apo import mcp_server

    conn, _, task = setup
    monkeypatch.setattr(mcp_server.claude_runtime, "current_session_id", lambda pid=None: "agent-1")
    assert mcp_server.claim_task(task_id=task) == {"claimed": True}
    assert mcp_server.retry_task(task_id=task, note="stuck") == {"retried": True}
    mcp_server.claim_task(task_id=task)
    assert mcp_server.complete_task(task_id=task, note="x", outcome="partial", not_done="y") == {"completed": True}
    assert db.get_decision_request(conn, task)["completion"]["outcome"] == "partial"


def test_cli_retry(setup, db_path):
    conn, _, task = setup
    db.claim_task(conn, task, "s1")
    env = {**os.environ, "APO_DB": str(db_path)}
    out = run_cli("retry", str(task), "--note", "died", env=env)
    assert out.returncode == 0, out.stderr
    assert db.get_decision_request(db.get_connection(), task)["status"] == "open"
    assert run_cli("retry", str(task), env=env).returncode == 1


def test_cli_complete_accepts_structured_options(setup, db_path):
    conn, _, task = setup
    env = {**os.environ, "APO_DB": str(db_path)}
    out = run_cli("complete", str(task), "--note", "n", "--outcome", "partial", "--not-done", "docs", env=env)
    assert out.returncode == 0, out.stderr
    assert db.get_decision_request(db.get_connection(), task)["completion"]["not_done"] == "docs"


def test_item_page_shows_history_completion_and_retry(setup):
    from apo.web import app

    conn, _, task = setup
    client = TestClient(app.app)
    db.claim_task(conn, task, "s1")
    html = client.get(f"/decisions/{task}").text
    assert "Retry" in html and "History" in html and "Claimed" in html
    r = client.post(f"/items/{task}/retry", data={"note": "stuck"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.get_decision_request(conn, task)["status"] == "open"
    db.complete_task(conn, task, note="n", outcome="partial", not_done="docs")
    html = client.get(f"/decisions/{task}").text
    assert "Partly done" in html and "docs" in html and "Retried" in html
