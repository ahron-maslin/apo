"""The Now queue: everything that needs the human, across every project, ranked by
what waiting costs. Projects are a label on each row, not a place to navigate to."""
from datetime import datetime, timedelta, timezone

from apo import db


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _project(conn, name, path):
    project = db.get_or_create_project(conn, name)
    repo_id = db.ensure_repository_for_project(conn, project["id"], str(path))
    return project, repo_id


def _keys(items):
    return [i["key"] for i in items]


def test_a_permission_wait_and_a_request_from_a_resting_agent_are_your_move(db_path, tmp_path):
    conn = db.get_connection()
    project, repo_id = _project(conn, "acme", tmp_path / "acme")
    db.register_session(conn, "waiting", str(tmp_path / "acme"))
    db.session_waiting(conn, "waiting", "permission", "Bash: make deploy")
    db.register_session(conn, "resting", str(tmp_path / "acme"))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo_id, title="Pick a queue", question="Q?",
        origin_session_id="resting",
    )
    db.session_stop(conn, "resting")

    now = db.now_queue(conn)

    assert set(_keys(now["your_move"])) == {"s:waiting", f"r:{decision_id}"}
    assert now["asks"] == []
    permission = next(i for i in now["your_move"] if i["key"] == "s:waiting")
    assert permission["project_name"] == "acme"
    assert "make deploy" in permission["reason"]


def test_a_request_whose_agent_is_still_working_is_an_ask_not_a_blocker(db_path, tmp_path):
    conn = db.get_connection()
    project, repo_id = _project(conn, "acme", tmp_path / "acme")
    db.register_session(conn, "busy", str(tmp_path / "acme"))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo_id, title="Pick a queue", question="Q?",
        origin_session_id="busy",
    )
    db.session_heartbeat(conn, "busy")

    now = db.now_queue(conn)

    assert _keys(now["asks"]) == [f"r:{decision_id}"]
    assert f"r:{decision_id}" not in _keys(now["your_move"])


def test_cheap_blocking_moves_rank_above_slow_unblocking_ones(db_path, tmp_path):
    conn = db.get_connection()
    project, repo_id = _project(conn, "acme", tmp_path / "acme")
    slow = db.create_action_request(conn, project_id=project["id"], repository_id=repo_id, title="Rotate key", question="Q?")
    db.register_session(conn, "waiting", str(tmp_path / "acme"))
    db.session_waiting(conn, "waiting", "permission", "Bash: npm test")

    order = _keys(db.now_queue(conn)["your_move"])

    assert order.index("s:waiting") < order.index(f"r:{slow}")


def test_waiting_longer_raises_an_item(db_path, tmp_path):
    conn = db.get_connection()
    project, repo_id = _project(conn, "acme", tmp_path / "acme")
    fresh = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Fresh", question="Q?")
    old = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Old", question="Q?")
    conn.execute("UPDATE decision_requests SET created_at = ? WHERE id = ?", (_ago(days=3), old))
    conn.commit()

    assert _keys(db.now_queue(conn)["your_move"])[:2] == [f"r:{old}", f"r:{fresh}"]


def test_answers_nothing_will_pick_up_are_your_move_the_rest_are_in_transit(db_path, tmp_path):
    conn = db.get_connection()
    project, repo_id = _project(conn, "acme", tmp_path / "acme")
    stuck = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Stuck", question="Q?")
    moving = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Moving", question="Q?")
    db.answer_decision_request(conn, stuck, "yes")
    db.answer_decision_request(conn, moving, "yes")
    conn.execute("UPDATE decision_requests SET answered_at = ? WHERE id = ?", (_ago(hours=5), stuck))
    conn.commit()

    now = db.now_queue(conn)

    stuck_item = next(i for i in now["your_move"] if i["key"] == f"t:{stuck}")
    assert stuck_item["resume_command"].startswith("cd ")
    assert _keys(now["in_transit"]) == [f"t:{moving}"]


def test_a_declined_answer_nothing_will_pick_up_is_in_transit_not_your_move(db_path, tmp_path):
    conn = db.get_connection()
    (tmp_path / "acme").mkdir()
    project, repo_id = _project(conn, "acme", tmp_path / "acme")
    decision = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Sell?", question="Q?")
    action = db.create_action_request(conn, project_id=project["id"], repository_id=repo_id, title="Rotate key", question="Q?")
    for item_id in (decision, action):
        text, kind, _ = db.compose_answer(db.get_decision_request(conn, item_id), "decline")
        db.answer_decision_request(conn, item_id, text, answer_kind=kind)
        conn.execute("UPDATE decision_requests SET answered_at = ? WHERE id = ?", (_ago(hours=5), item_id))
    conn.commit()

    now = db.now_queue(conn)

    assert now["your_move"] == []
    assert set(_keys(now["in_transit"])) == {f"t:{decision}", f"t:{action}"}
    assert all(i["reason"].startswith("Declined") for i in now["in_transit"])
    assert next(p for p in db.project_overview(conn, queue=now) if p["name"] == "acme")["health"] == "in_transit"


def test_cancelled_requests_and_tasks_are_never_your_move(db_path, tmp_path):
    conn = db.get_connection()
    project, repo_id = _project(conn, "acme", tmp_path / "acme")
    decision = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Sell?", question="Q?")
    task = db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Fix", question="Q?",
                                      kind="task")
    for item_id in (decision, task):
        assert db.cancel_request(conn, item_id, "Not needed")
        conn.execute("UPDATE decision_requests SET created_at = ?, resolved_at = ? WHERE id = ?",
                     (_ago(days=5), _ago(hours=5), item_id))
    conn.commit()

    now = db.now_queue(conn)

    assert now["your_move"] == [] and now["asks"] == [] and now["in_transit"] == []
    assert db.project_work(conn, project["id"])["needs_you"] == []


def test_a_finished_turn_is_up_for_review_until_you_engage(db_path, tmp_path):
    conn = db.get_connection()
    _project(conn, "acme", tmp_path / "acme")
    db.register_session(conn, "done", str(tmp_path / "acme"))
    db.session_heartbeat(conn, "done")
    db.session_stop(conn, "done")

    assert _keys(db.now_queue(conn)["review"]) == ["v:done"]

    db.mark_session_reviewed(conn, "done")
    assert db.now_queue(conn)["review"] == []

    db.session_prompt_submitted(conn, "done", "next thing", str(tmp_path / "acme"))
    db.session_stop(conn, "done")
    assert _keys(db.now_queue(conn)["review"]) == ["v:done"]
    db.session_prompt_submitted(conn, "done", "and another", str(tmp_path / "acme"))
    assert db.now_queue(conn)["review"] == []


def test_problems_are_faults_only_you_can_recover(db_path, tmp_path):
    conn = db.get_connection()
    _project(conn, "acme", tmp_path / "acme")
    db.register_session(conn, "crashed", str(tmp_path / "acme"))
    db.session_heartbeat(conn, "crashed")
    db.session_end(conn, "crashed", "process_gone_mid_turn")
    db.register_session(conn, "errored", str(tmp_path / "acme"))
    db.session_stop_failure(conn, "errored", "overloaded")
    for name in ("one", "two"):
        project = db.get_or_create_project(conn, name)
        db.create_repository(conn, project["id"], name, str(tmp_path / "shared"))

    keys = _keys(db.now_queue(conn)["problems"])

    assert "s:crashed" in keys and "s:errored" in keys
    assert any(k.startswith("folder:") for k in keys)


def test_archived_projects_stay_out_of_the_queue(db_path, tmp_path):
    conn = db.get_connection()
    project, repo_id = _project(conn, "old", tmp_path / "old")
    db.create_decision_request(conn, project_id=project["id"], repository_id=repo_id, title="Q", question="Q?")
    conn.execute("UPDATE projects SET archived_at = ?", (db._now(),))
    conn.commit()

    assert db.now_queue(conn)["your_move"] == []


def test_the_queue_says_how_long_your_moves_take_and_what_is_quiet(db_path, tmp_path):
    conn = db.get_connection()
    project, repo_id = _project(conn, "acme", tmp_path / "acme")
    db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo_id, title="Q", question="Q?", recommendation="Do A",
        confidence=90,
    )
    db.register_session(conn, "worker", str(tmp_path / "acme"))
    db.session_heartbeat(conn, "worker")

    now = db.now_queue(conn)

    assert now["your_move_minutes"] == 1
    assert now["quiet"]["working"] == 1


def test_a_folder_missing_on_disk_is_a_problem_that_points_at_its_new_path(db_path, tmp_path):
    from fastapi.testclient import TestClient

    from apo.web import app

    conn = db.get_connection()
    (tmp_path / "acme").mkdir()
    project, repo_id = _project(conn, "acme", tmp_path / "acme")
    (tmp_path / "old").mkdir()
    archived, _ = _project(conn, "old", tmp_path / "old")
    conn.execute("UPDATE projects SET archived_at = ? WHERE id = ?", (db._now(), archived["id"]))
    conn.commit()
    assert not [p for p in db.now_queue(conn)["problems"] if p["kind"] == "missing_folder"]

    (tmp_path / "acme").rename(tmp_path / "acme-shop")
    (tmp_path / "old").rmdir()

    problems = [p for p in db.now_queue(conn)["problems"] if p["kind"] == "missing_folder"]
    assert [p["key"] for p in problems] == [f"missing:{repo_id}"]
    assert problems[0]["project_name"] == "acme" and str(tmp_path / "acme") in problems[0]["reason"]
    assert [p["key"] for p in db.now_queue(conn, project_id=project["id"])["problems"]] == [f"missing:{repo_id}"]

    html = TestClient(app.app).get("/").text
    assert f'href="/projects/{project["id"]}/settings#folders"' in html
