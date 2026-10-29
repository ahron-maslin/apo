"""A verdict for a session that ended, from data already held (no model call), and the
parent/child link between a session and the ones it dispatched."""
import re

import pytest
from fastapi.testclient import TestClient

from apo import db


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def _setup(tmp_path, sid="s-1"):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, sid, str(tmp_path))
    return conn, project


def _s(conn, sid="s-1"):
    return {s["claude_session_id"]: s for s in db.list_sessions(conn, include_ended=True)}[sid]


def _task(conn, project, title="T"):
    return db.add_work(conn, project["id"], "task", title)


def test_a_running_session_has_no_verdict(db_path, tmp_path):
    conn, _ = _setup(tmp_path)
    s = _s(conn)
    assert s["verdict"] is None and s["verdict_reason"] is None


def test_a_session_killed_mid_turn_crashed(db_path, tmp_path):
    conn, _ = _setup(tmp_path)
    db.session_end(conn, "s-1", "process_gone_mid_turn")
    s = _s(conn)
    assert s["verdict"] == "crashed" and s["verdict_reason"]


def test_a_session_with_nothing_to_judge_by_is_unknown(db_path, tmp_path):
    conn, _ = _setup(tmp_path)
    db.session_end(conn, "s-1", "other")
    assert _s(conn)["verdict"] == "unknown"


def test_a_completed_task_makes_it_done(db_path, tmp_path):
    conn, project = _setup(tmp_path)
    task = _task(conn, project)
    db.claim_task(conn, task, "s-1")
    db.complete_task(conn, task, "ok")
    db.session_end(conn, "s-1", "other")
    assert _s(conn)["verdict"] == "done"


def test_a_claimed_task_left_open_is_judged_before_it_is_released(db_path, tmp_path):
    conn, project = _setup(tmp_path)
    task = _task(conn, project, "Wire the queue")
    db.claim_task(conn, task, "s-1")
    db.session_end(conn, "s-1", "other")
    s = _s(conn)
    assert s["verdict"] == "abandoned" and "Wire the queue" in s["verdict_reason"]
    assert db.get_decision_request(conn, task)["status"] == "open"


def test_some_done_and_some_left_is_partial(db_path, tmp_path):
    conn, project = _setup(tmp_path)
    done, left = _task(conn, project), _task(conn, project)
    db.claim_task(conn, done, "s-1")
    db.complete_task(conn, done)
    db.claim_task(conn, left, "s-1")
    db.session_end(conn, "s-1", "other")
    assert _s(conn)["verdict"] == "partial"


def test_unchecked_checklist_items_make_it_partial(db_path, tmp_path):
    conn, _ = _setup(tmp_path)
    sid = _s(conn)["id"]
    first = db.add_checklist_item(conn, sid, "one")
    db.add_checklist_item(conn, sid, "two")
    db.complete_checklist_item_by_id(conn, first)
    db.session_end(conn, "s-1", "other")
    s = _s(conn)
    assert s["verdict"] == "partial" and "1 of 2" in s["verdict_reason"]


def test_an_open_blocker_alone_is_abandoned(db_path, tmp_path):
    conn, project = _setup(tmp_path)
    db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="s-1")
    db.session_end(conn, "s-1", "other")
    s = _s(conn)
    assert s["verdict"] == "abandoned" and "decision" in s["verdict_reason"].lower()


def test_finished_work_that_is_not_merged_is_partial(db_path, tmp_path):
    conn, project = _setup(tmp_path)
    task = _task(conn, project)
    db.claim_task(conn, task, "s-1")
    db.complete_task(conn, task)
    conn.execute("UPDATE decision_requests SET work_location = ? WHERE id = ?",
                 ('{"branch": "x", "commit": "abc1234", "default_branch": "master", "merged": false}', task))
    conn.commit()
    db.session_end(conn, "s-1", "other")
    s = _s(conn)
    assert s["verdict"] == "partial" and "not merged" in s["verdict_reason"]


def test_old_ended_sessions_are_backfilled_when_listed(db_path, tmp_path):
    conn, _ = _setup(tmp_path)
    conn.execute("UPDATE sessions SET status = 'ended', ended_at = '2026-01-01T00:00:00+00:00', "
                 "end_reason = 'process_gone_mid_turn'")
    conn.commit()
    assert _s(conn)["verdict"] == "crashed"
    assert db.get_session(conn, "s-1")["verdict"] == "crashed"


def test_resuming_a_session_clears_its_verdict(db_path, tmp_path):
    conn, _ = _setup(tmp_path)
    db.session_end(conn, "s-1", "other")
    db.register_session(conn, "s-1", str(tmp_path))
    assert _s(conn)["verdict"] is None


def test_the_verdict_is_in_the_api_and_on_the_pages(client, db_path, tmp_path):
    conn, project = _setup(tmp_path)
    db.claim_task(conn, _task(conn, project), "s-1")
    db.session_end(conn, "s-1", "exit")
    row = _s(conn)
    assert row["verdict"] == "abandoned"
    page = client.get(f"/sessions/{row['id']}").text
    assert "verdict-abandoned" in page
    fleet = client.get("/projects/agents").text
    assert "verdict-abandoned" in fleet
    db.register_session(conn, "kid", str(tmp_path))
    conn.execute("UPDATE sessions SET parent_session_id = 's-1' WHERE claude_session_id = 'kid'")
    conn.commit()
    api = {s["claude_session_id"]: s for s in client.get("/api/sessions").json()}
    assert api["kid"]["verdict"] is None and api["kid"]["parent"]["claude_session_id"] == "s-1"
    assert api["kid"]["children"] == [] and "verdict_reason" in api["kid"]


def test_a_dispatched_session_is_linked_to_the_one_that_dispatched_it(db_path, tmp_path):
    conn, project = _setup(tmp_path)
    task = _task(conn, project)
    db.record_dispatch(conn, task, "ab12cd34", resumed=False, parent_session_id="s-1")
    db.register_session(conn, "child", str(tmp_path))
    db.claim_task(conn, task, "child")
    child = _s(conn, "child")
    assert child["parent_session_id"] == "s-1"
    assert child["parent"]["claude_session_id"] == "s-1"
    parent = _s(conn, "s-1")
    assert parent["children_summary"] == "1 running"
    assert [c["claude_session_id"] for c in parent["children"]] == ["child"]


def test_a_dispatched_session_is_linked_by_its_agent_id_too(db_path, tmp_path):
    conn, project = _setup(tmp_path)
    task = _task(conn, project)
    db.record_dispatch(conn, task, "ab12cd34", resumed=False, parent_session_id="s-1")
    db.register_session(conn, "child", str(tmp_path))
    agents = [{"sessionId": "child", "id": "ab12cd34", "kind": "background", "status": "busy", "pid": 1}]
    db.reconcile_live_sessions(conn, agents)
    assert _s(conn, "child")["parent_session_id"] == "s-1"


def test_a_resume_has_no_child(db_path, tmp_path):
    conn, project = _setup(tmp_path)
    task = _task(conn, project)
    db.record_dispatch(conn, task, "ab12cd34", resumed=True, parent_session_id="s-1")
    assert db.get_decision_request(conn, task)["dispatch_parent_session_id"] is None


def test_the_rollup_counts_running_and_failed_children(db_path, tmp_path):
    conn, _ = _setup(tmp_path)
    for name in ("a", "b", "c"):
        db.register_session(conn, name, str(tmp_path))
        conn.execute("UPDATE sessions SET parent_session_id = 's-1' WHERE claude_session_id = ?", (name,))
    conn.commit()
    db.session_end(conn, "c", "process_gone_mid_turn")
    assert _s(conn, "s-1")["children_summary"] == "2 running, 1 failed"


def test_the_ended_recently_list_shows_each_sessions_verdict_and_reason(client, db_path, tmp_path):
    conn, project = _setup(tmp_path)
    db.claim_task(conn, _task(conn, project), "s-1")
    db.session_end(conn, "s-1", "exit")

    html = client.get(f"/projects/{project['id']}/agents").text
    ended = html[html.index("Ended recently"):]

    assert "verdict-abandoned" in ended and "unfinished" in ended


def test_a_crashed_session_shows_one_crashed_badge_not_two(client, db_path, tmp_path):
    conn, project = _setup(tmp_path)
    db.session_end(conn, "s-1", "process_gone_mid_turn")
    sid = _s(conn)["id"]

    for page in (f"/sessions/{sid}", f"/projects/{project['id']}/agents"):
        html = client.get(page).text
        badges = [m for m in re.findall(r'class="badge [^"]*"[^>]*>(?:<span[^>]*></span>)?(?:<span[^>]*>)?Crashed', html)]
        assert len(badges) == 1, page
        assert _s(conn)["verdict_reason"] in html
