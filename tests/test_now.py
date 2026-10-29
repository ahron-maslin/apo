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
