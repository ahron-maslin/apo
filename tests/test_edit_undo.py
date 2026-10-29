"""Editing a filed item, undoing a dashboard action, and reopening a closed item."""
import json
from urllib.parse import unquote

from fastapi.testclient import TestClient

from apo import db


def _file(conn, tmp_path, **kw):
    project = db.get_or_create_project(conn, "acme")
    repo_id = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    kw.setdefault("title", "Pick a queue")
    kw.setdefault("question", "Which queue?")
    return db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, **kw)


def _index(tmp_path):
    return (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()


def _events(conn, verb):
    return [r["summary"] for r in conn.execute("SELECT summary FROM events WHERE verb = ?", (verb,))]


def _linear(config_module, sources_module):
    config_module.LINEAR_API_KEY = "k"
    config_module.LINEAR_TEAM = "ENG"
    sources_module.linear_client.create_issue = lambda p, d: {"id": "iss", "identifier": "E-1", "url": "u"}


# --- edit ---

def test_edit_changes_text_rewrites_the_repo_and_logs(db_path, tmp_path):
    conn = db.get_connection()
    item = _file(conn, tmp_path)
    assert db.edit_item(conn, item, title="Pick a job queue", description="Short", details="Longer body")
    d = db.get_decision_request(conn, item)
    assert (d["title"], d["summary"], d["question"]) == ("Pick a job queue", "Short", "Longer body")
    assert "Pick a job queue" in _index(tmp_path)
    files = list((tmp_path / ".claude" / "decisions").glob("0*.md"))
    assert [f.name for f in files] == [f"{item:04d}-pick-a-job-queue.md"]
    assert _events(conn, "item.edited")
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE decision_request_id = ? AND event_type = 'edited'", (item,)
    ).fetchone()


def test_edit_without_changes_or_with_blank_title_is_refused(db_path, tmp_path):
    conn = db.get_connection()
    item = _file(conn, tmp_path)
    assert db.edit_item(conn, item, title="Pick a queue") is False
    try:
        db.edit_item(conn, item, title="   ")
        raise AssertionError("blank title accepted")
    except ValueError:
        pass


def test_editing_a_task_description_keeps_its_brief_in_step(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    item = db.create_task(conn, project_id=project["id"], title="T", detail="old brief")
    db.edit_item(conn, item, description="new brief")
    d = db.get_decision_request(conn, item)
    assert d["summary"] == d["question"] == "new brief"


def test_edit_pushes_to_linear(db_path, config_module, sources_module, tmp_path):
    _linear(config_module, sources_module)
    calls = []
    sources_module.linear_client.update_issue = lambda issue_id, decision: calls.append((issue_id, decision["title"]))
    conn = db.get_connection()
    item = _file(conn, tmp_path)
    db.edit_item(conn, item, title="New title")
    assert calls == [("iss", "New title")]


def test_edit_while_linear_is_down_queues_and_retries(db_path, config_module, sources_module, tmp_path):
    _linear(config_module, sources_module)

    def down(issue_id, decision):
        raise RuntimeError("linear is down")
    sources_module.linear_client.update_issue = down
    conn = db.get_connection()
    item = _file(conn, tmp_path)
    assert db.edit_item(conn, item, title="New title")
    d = db.get_decision_request(conn, item)
    assert d["title"] == "New title"
    assert "linear is down" in d["linear_edit_error"]
    assert conn.execute("SELECT 1 FROM sync_errors WHERE source = 'linear_edit'").fetchone()

    calls = []
    sources_module.linear_client.update_issue = lambda issue_id, decision: calls.append(decision["title"])
    conn.execute("UPDATE decision_requests SET linear_next_retry_at = NULL")
    conn.commit()
    sources_module.LinearSource().retry_pending()
    assert calls == ["New title"]
    assert db.get_decision_request(conn, item)["linear_edit_error"] is None


def test_edit_route(db_path, tmp_path):
    from apo.web import app as app_module
    client = TestClient(app_module.app)
    conn = db.get_connection()
    item = _file(conn, tmp_path)
    r = client.post(f"/decisions/{item}/edit", data={"title": "Renamed", "description": "d", "details": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert db.get_decision_request(conn, item)["title"] == "Renamed"
    r = client.post(f"/decisions/{item}/edit", data={"title": " "}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert db.get_decision_request(conn, item)["title"] == "Renamed"


# --- undo ---

def test_undo_done_on_a_task_restores_open(db_path, tmp_path):
    conn = db.get_connection()
    item = _file(conn, tmp_path, kind="task", requested_by="human")
    db.snapshot_for_undo(conn, item, "done")
    db.complete_step(conn, item, by="human", note="finished")
    assert db.get_decision_request(conn, item)["status"] == "resolved"
    assert db.undo_last(conn, item)
    d = db.get_decision_request(conn, item)
    assert d["status"] == "open" and d["resolved_at"] is None and d["answer_text"] is None
    assert d["undo_snapshot"] is None
    assert _events(conn, "item.undone")
    assert db.undo_last(conn, item) is False


def test_undo_answer_restores_open_decision(db_path, tmp_path):
    conn = db.get_connection()
    item = _file(conn, tmp_path)
    db.snapshot_for_undo(conn, item, "done")
    db.answer_decision_request(conn, item, "Redis")
    assert db.undo_last(conn, item)
    d = db.get_decision_request(conn, item)
    assert d["status"] == "open" and d["answer_text"] is None and d["answered_at"] is None
    assert "| OPEN |" in _index(tmp_path)


def test_undo_cancel_and_snooze(db_path, tmp_path):
    conn = db.get_connection()
    item = _file(conn, tmp_path, kind="task", requested_by="human")
    db.snapshot_for_undo(conn, item, "cancel")
    db.cancel_request(conn, item, "nope")
    assert db.undo_last(conn, item)
    d = db.get_decision_request(conn, item)
    assert d["status"] == "open" and d["cancel_reason"] is None

    other = _file(conn, tmp_path, title="Other")
    db.snapshot_for_undo(conn, other, "snooze")
    db.snooze_request(conn, other, until="2999-01-01T00:00:00+00:00")
    assert db.undo_last(conn, other)
    d = db.get_decision_request(conn, other)
    assert d["snoozed_until"] is None and d["snooze_condition"] is None


def test_undo_refuses_when_state_moved_on(db_path, tmp_path):
    conn = db.get_connection()
    item = _file(conn, tmp_path)
    db.snapshot_for_undo(conn, item, "snooze")
    assert db.undo_last(conn, item) is False


def test_undo_done_reopens_the_linear_issue(db_path, config_module, sources_module, tmp_path):
    _linear(config_module, sources_module)
    moves = []
    sources_module.linear_client.move_to_state = lambda issue, state, team_id=None: moves.append(state)
    sources_module.linear_client.create_comment = lambda *a: None
    conn = db.get_connection()
    item = _file(conn, tmp_path)
    db.snapshot_for_undo(conn, item, "done")
    db.answer_decision_request(conn, item, "Redis")
    db.undo_last(conn, item)
    assert moves == ["completed", "unstarted"]


# --- reopen ---

def test_reopen_an_applied_decision_goes_back_to_answered_keeping_the_answer(db_path, tmp_path):
    conn = db.get_connection()
    item = _file(conn, tmp_path, requested_by="agent")
    db.answer_decision_request(conn, item, "Redis")
    db.resolve_decision_request(conn, item)
    assert db.reopen_item(conn, item) == "answered"
    d = db.get_decision_request(conn, item)
    assert d["status"] == "answered" and d["answer_text"] == "Redis" and d["resolved_at"] is None
    assert d["delivery"] == "pending"
    assert "| ANSWERED |" in _index(tmp_path)
    assert _events(conn, "item.reopened")


def test_reopen_a_human_closed_decision_goes_open_and_keeps_the_old_answer(db_path, tmp_path):
    conn = db.get_connection()
    item = _file(conn, tmp_path, requested_by="human")
    db.answer_decision_request(conn, item, "Redis")
    assert db.get_decision_request(conn, item)["status"] == "resolved"
    assert db.reopen_item(conn, item) == "open"
    d = db.get_decision_request(conn, item)
    assert d["answer_text"] is None and d["previous_answer"] == "Redis"
    assert db.answer_decision_request(conn, item, "Postgres")


def test_reopen_a_completed_task_and_a_cancelled_item(db_path, tmp_path):
    conn = db.get_connection()
    t = _file(conn, tmp_path, kind="task", requested_by="human")
    db.complete_task(conn, t, note="did it", by="human")
    assert db.reopen_item(conn, t) == "open"
    assert db.get_decision_request(conn, t)["answer_text"] == "did it"
    c = _file(conn, tmp_path, title="C", kind="task", requested_by="human")
    db.cancel_request(conn, c, "dropped")
    assert db.reopen_item(conn, c) == "open"
    assert db.get_decision_request(conn, c)["status"] == "open"


def test_reopen_refuses_non_final_items(db_path, tmp_path):
    conn = db.get_connection()
    item = _file(conn, tmp_path)
    assert db.reopen_item(conn, item) is None


def test_routes_set_an_undo_cookie_and_undo_works(db_path, tmp_path):
    from apo.web import app as app_module
    client = TestClient(app_module.app)
    conn = db.get_connection()
    item = _file(conn, tmp_path, kind="task", requested_by="human")
    r = client.post(f"/items/{item}/done", data={}, follow_redirects=False)
    cookie = json.loads(unquote(r.cookies["ado_undo"]))
    assert cookie["id"] == item and cookie["label"]
    r = client.post(f"/items/{item}/undo", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert db.get_decision_request(conn, item)["status"] == "open"
    client.post(f"/items/{item}/cancel", data={}, follow_redirects=False)
    assert db.get_decision_request(conn, item)["status"] == "cancelled"
    client.post(f"/items/{item}/reopen", data={}, follow_redirects=False)
    assert db.get_decision_request(conn, item)["status"] == "open"
    page = client.get(f"/decisions/{item}")
    assert page.status_code == 200 and f"/decisions/{item}/edit" in page.text


def test_api_edit_and_reopen(db_path, tmp_path):
    from apo.web import app as app_module
    client = TestClient(app_module.app)
    conn = db.get_connection()
    item = _file(conn, tmp_path, requested_by="human")
    assert client.patch(f"/api/decisions/{item}", json={"title": "X"}).json()["changed"] is True
    db.answer_decision_request(conn, item, "a")
    assert client.post(f"/api/decisions/{item}/reopen").json()["status"] == "open"
    assert client.post(f"/api/decisions/{item}/reopen").status_code == 409
