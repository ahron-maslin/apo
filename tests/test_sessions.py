"""Session fleet tracking + checklists.

Every hook-facing function here (register_session, session_heartbeat, session_stop,
session_end, record_checklist_item, complete_checklist_item) must behave exactly like
its hook wrapper needs: never raise on a missing/unknown session, since the hook
scripts calling these are wrapped in a fail-open contract (see tests/test_cli.py) and
these are the layer underneath that contract.
"""
from apo import db


def test_register_session_creates_row_running(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", str(tmp_path))

    db.register_session(conn, "sess-1", str(tmp_path))

    sessions = db.list_sessions(conn)
    assert len(sessions) == 1
    assert sessions[0]["claude_session_id"] == "sess-1"
    assert sessions[0]["status"] == "running"
    assert sessions[0]["project_id"] == project["id"]
    assert sessions[0]["repository_id"] == repo["id"]


def test_register_session_auto_registers_a_repo_when_none_matches(db_path, tmp_path):
    """A session whose cwd matches no registered repository gets one created on
    the spot, named from the directory's own basename, rather than being
    stranded in the fleet view's "Unregistered" bucket forever just because
    nothing's filed a decision from that project yet."""
    conn = db.get_connection()
    cwd = str(tmp_path / "my-cool-project")

    db.register_session(conn, "sess-1", cwd)

    s = db.list_sessions(conn)[0]
    assert s["project_id"] is not None
    assert s["repository_id"] is not None
    assert s["project_name"] == "my-cool-project"
    assert db.get_repository(conn, s["repository_id"])["local_path"] == cwd


def test_register_session_auto_register_is_idempotent_across_resumes(db_path, tmp_path):
    conn = db.get_connection()
    cwd = str(tmp_path / "my-cool-project")

    db.register_session(conn, "sess-1", cwd)
    db.register_session(conn, "sess-2", cwd)

    sessions = {s["claude_session_id"]: s for s in db.list_sessions(conn)}
    assert sessions["sess-1"]["repository_id"] == sessions["sess-2"]["repository_id"]
    assert len(db.list_sessions(conn)) == 2


def test_register_session_attaches_to_an_existing_same_name_project(db_path, tmp_path):
    """If a project already exists under this name (e.g. created earlier by
    filing a decision from it) but has no repository registered yet, the new
    repo attaches to that project instead of creating a duplicate."""
    conn = db.get_connection()
    cwd = str(tmp_path / "acme")
    existing_project = db.get_or_create_project(conn, "acme")

    db.register_session(conn, "sess-1", cwd)

    s = db.list_sessions(conn)[0]
    assert s["project_id"] == existing_project["id"]


def test_register_session_without_a_cwd_leaves_project_none(db_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", None)

    sessions = db.list_sessions(conn)
    assert sessions[0]["project_id"] is None
    assert sessions[0]["repository_id"] is None


def test_register_session_upserts_and_preserves_current_blocker(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="sess-1",
    )
    assert db.list_sessions(conn)[0]["status"] == "blocked"

    # A resumed session (another SessionStart) must not silently forget it was blocked.
    db.register_session(conn, "sess-1", str(tmp_path))

    sessions = db.list_sessions(conn)
    assert len(sessions) == 1
    assert sessions[0]["current_blocker_id"] == decision_id


def test_session_heartbeat_updates_last_seen_and_forces_running(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    conn.execute("UPDATE sessions SET status = 'idle' WHERE claude_session_id = ?", ("sess-1",))
    conn.commit()

    db.session_heartbeat(conn, "sess-1", str(tmp_path))

    assert db.list_sessions(conn)[0]["status"] == "running"


def test_session_heartbeat_self_heals_missing_session(db_path, tmp_path):
    conn = db.get_connection()
    db.session_heartbeat(conn, "sess-unknown", str(tmp_path))

    sessions = db.list_sessions(conn)
    assert len(sessions) == 1
    assert sessions[0]["claude_session_id"] == "sess-unknown"
    assert sessions[0]["status"] == "running"


def test_session_stop_marks_idle_when_no_blocker(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    db.session_stop(conn, "sess-1")

    assert db.list_sessions(conn)[0]["status"] == "idle"


def test_session_stop_marks_blocked_when_blocker_open(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="sess-1",
    )

    db.session_stop(conn, "sess-1")

    assert db.list_sessions(conn)[0]["status"] == "blocked"


def test_session_stop_marks_idle_when_blocker_already_resolved(db_path, tmp_path):
    """Defensive: current_blocker_id should already be cleared by answer_decision_request
    by the time Stop fires, but session_stop must not trust a stale pointer."""
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="sess-1",
    )
    # Force a stale pointer: blocker resolved, but current_blocker_id not cleared.
    conn.execute(
        "UPDATE decision_requests SET status = 'answered' WHERE id = ?", (decision_id,)
    )
    conn.commit()

    db.session_stop(conn, "sess-1")

    assert db.list_sessions(conn)[0]["status"] == "idle"


def test_session_stop_noop_for_unknown_session(db_path):
    conn = db.get_connection()
    db.session_stop(conn, "sess-unknown")  # must not raise
    assert db.list_sessions(conn) == []


def test_session_end_marks_ended_with_reason(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    db.session_end(conn, "sess-1", "prompt_input_exit")

    sessions = db.list_sessions(conn, include_ended=True)
    assert sessions[0]["status"] == "ended"
    assert sessions[0]["end_reason"] == "prompt_input_exit"
    assert sessions[0]["ended_at"] is not None


def test_session_end_noop_for_unknown_session(db_path):
    conn = db.get_connection()
    db.session_end(conn, "sess-unknown", "other")  # must not raise


def test_create_decision_request_with_origin_session_id_marks_session_blocked(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))

    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="sess-1",
    )

    session = db.list_sessions(conn)[0]
    assert session["status"] == "blocked"
    assert session["current_blocker_id"] == decision_id


def test_create_decision_request_without_registered_session_does_not_raise(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    # origin_session_id doesn't correspond to any registered session row.
    db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="sess-ghost",
    )


def test_create_action_request_defaults_to_action_kind_and_blocks_session(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))

    action_id = db.create_action_request(
        conn, project_id=project["id"], title="Grant S3 access", question="Please grant write access to the bucket.",
        origin_session_id="sess-1",
    )

    row = db.get_decision_request(conn, action_id)
    assert row["kind"] == "action"
    assert row["options"] is None
    assert row["recommendation"] is None
    assert row["confidence"] is None

    session = db.list_sessions(conn)[0]
    assert session["status"] == "blocked"
    assert session["current_blocker_id"] == action_id


def test_create_decision_request_defaults_to_decision_kind(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    assert db.get_decision_request(conn, decision_id)["kind"] == "decision"


def test_answer_decision_request_clears_session_blocker(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="sess-1",
    )
    assert db.list_sessions(conn)[0]["status"] == "blocked"

    db.answer_decision_request(conn, decision_id, "An answer")

    session = db.list_sessions(conn)[0]
    assert session["status"] == "idle"
    assert session["current_blocker_id"] is None


def test_close_decision_request_clears_session_blocker(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="sess-1",
    )

    db.close_decision_request(conn, decision_id, "Closed — project archived.")

    session = db.list_sessions(conn)[0]
    assert session["status"] == "idle"
    assert session["current_blocker_id"] is None


def _blocked_session_with_requests(conn, tmp_path, n):
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    return [
        db.create_decision_request(
            conn, project_id=project["id"], title=f"Q{i}", question="Q?", origin_session_id="sess-1",
        )
        for i in range(n)
    ]


def test_session_stays_blocked_until_every_request_it_filed_is_answered(db_path, tmp_path):
    """Agents batch-file: one live session filed three decisions in six seconds.
    Answering the last one filed must not mark the session unblocked while the other
    two are still open."""
    conn = db.get_connection()
    first, second, third = _blocked_session_with_requests(conn, tmp_path, 3)

    db.answer_decision_request(conn, third, "c")
    session = db.list_sessions(conn)[0]
    assert session["status"] == "blocked"
    assert session["open_request_count"] == 2
    assert session["current_blocker_id"] == first

    db.answer_decision_request(conn, first, "a")
    db.answer_decision_request(conn, second, "b")
    session = db.list_sessions(conn)[0]
    assert session["status"] == "idle"
    assert session["open_request_count"] == 0
    assert session["current_blocker_id"] is None


def test_session_stop_stays_blocked_while_any_filed_request_is_open(db_path, tmp_path):
    conn = db.get_connection()
    first, second = _blocked_session_with_requests(conn, tmp_path, 2)
    db.answer_decision_request(conn, second, "b")
    db.session_heartbeat(conn, "sess-1", str(tmp_path))

    db.session_stop(conn, "sess-1")

    assert db.list_sessions(conn)[0]["status"] == "blocked"


def test_bind_request_to_session_moves_it_off_a_stale_session(db_path, tmp_path):
    """The MCP server's view of "which session am I" can be stale after /clear; the
    PostToolUse hook for the create call knows the real one and rebinds."""
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "stale", str(tmp_path))
    db.register_session(conn, "real", str(tmp_path))
    request_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="stale",
    )

    assert db.bind_request_to_session(conn, request_id, "real") is True

    by_id = {s["claude_session_id"]: s for s in db.list_sessions(conn)}
    assert by_id["real"]["open_request_count"] == 1
    assert by_id["real"]["current_blocker_id"] == request_id
    assert by_id["stale"]["status"] == "idle"
    assert by_id["stale"]["open_request_count"] == 0
    assert db.get_decision_request(conn, request_id)["origin_session_id"] == "real"


def test_bind_request_to_session_is_a_noop_when_already_bound(db_path, tmp_path):
    conn = db.get_connection()
    (request_id,) = _blocked_session_with_requests(conn, tmp_path, 1)
    assert db.bind_request_to_session(conn, request_id, "sess-1") is False
    assert db.bind_request_to_session(conn, 999, "sess-1") is False


def test_list_sessions_computes_display_status_for_decision_and_action(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-decision", str(tmp_path))
    db.register_session(conn, "sess-action", str(tmp_path))
    db.create_decision_request(
        conn, project_id=project["id"], title="D", question="D?", origin_session_id="sess-decision",
    )
    db.create_action_request(
        conn, project_id=project["id"], title="A", question="A?", origin_session_id="sess-action",
    )

    by_id = {s["claude_session_id"]: s for s in db.list_sessions(conn)}
    assert by_id["sess-decision"]["display_status"] == "blocked_decision"
    assert by_id["sess-action"]["display_status"] == "blocked_action"


def test_list_sessions_excludes_ended_unless_requested(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_end(conn, "sess-1", "logout")

    assert db.list_sessions(conn) == []
    assert len(db.list_sessions(conn, include_ended=True)) == 1


def test_list_sessions_flags_stale_running_session(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_heartbeat(conn, "sess-1")  # mid-turn; a session at its prompt is never stale
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(minutes=db.STALE_MINUTES + 1)).isoformat()
    conn.execute("UPDATE sessions SET last_seen_at = ? WHERE claude_session_id = ?", (old, "sess-1"))
    conn.commit()

    assert db.list_sessions(conn)[0]["stale"] is True


def test_list_sessions_fresh_session_is_not_stale(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    assert db.list_sessions(conn)[0]["stale"] is False


def test_list_sessions_does_not_flag_blocked_session_as_stale(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="sess-1",
    )
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(minutes=db.STALE_MINUTES + 1)).isoformat()
    conn.execute("UPDATE sessions SET last_seen_at = ? WHERE claude_session_id = ?", (old, "sess-1"))
    conn.commit()

    session = db.list_sessions(conn)[0]
    assert session["status"] == "blocked"
    assert session["stale"] is False


# --- Checklists ---


def test_record_checklist_item_creates_pending_item(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    db.record_checklist_item(conn, "sess-1", "task-1", "Write tests", "Cover the happy path")

    session_id = db.list_sessions(conn)[0]["id"]
    items = db.list_checklist_items(conn, session_id)
    assert len(items) == 1
    assert items[0]["title"] == "Write tests"
    assert items[0]["description"] == "Cover the happy path"
    assert items[0]["status"] == "pending"
    assert items[0]["completed_by"] is None


def test_record_checklist_item_noop_for_unknown_session(db_path):
    conn = db.get_connection()
    db.record_checklist_item(conn, "sess-ghost", "task-1", "Write tests")  # must not raise


def test_complete_checklist_item_marks_done_with_agent(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.record_checklist_item(conn, "sess-1", "task-1", "Write tests")

    completed = db.complete_checklist_item(conn, "sess-1", "task-1", completion_notes="Done, 12 tests added.")

    assert completed is True
    session_id = db.list_sessions(conn)[0]["id"]
    item = db.list_checklist_items(conn, session_id)[0]
    assert item["status"] == "done"
    assert item["completed_by"] == "agent"
    assert item["completion_notes"] == "Done, 12 tests added."
    assert item["completed_at"] is not None


def test_complete_checklist_item_noop_if_task_id_not_found(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    assert db.complete_checklist_item(conn, "sess-1", "task-unknown") is False


def test_add_checklist_item_manual_creates_item_with_no_claude_task_id(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    session_id = db.list_sessions(conn)[0]["id"]

    item_id = db.add_checklist_item(conn, session_id, "Manually added step")

    item = db.list_checklist_items(conn, session_id)[0]
    assert item["id"] == item_id
    assert item["claude_task_id"] is None
    assert item["status"] == "pending"


def test_complete_checklist_item_by_id_marks_done_with_human(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    session_id = db.list_sessions(conn)[0]["id"]
    item_id = db.add_checklist_item(conn, session_id, "Manually added step")

    completed = db.complete_checklist_item_by_id(conn, item_id)

    assert completed is True
    item = db.list_checklist_items(conn, session_id)[0]
    assert item["status"] == "done"
    assert item["completed_by"] == "human"


def test_list_checklist_items_orders_by_created_at(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    session_id = db.list_sessions(conn)[0]["id"]
    db.record_checklist_item(conn, "sess-1", "task-1", "First")
    db.record_checklist_item(conn, "sess-1", "task-2", "Second")

    items = db.list_checklist_items(conn, session_id)
    assert [i["title"] for i in items] == ["First", "Second"]


def test_record_checklist_item_is_idempotent_per_task_id(db_path, tmp_path):
    # TaskCreated and the PostToolUse(TaskCreate) sync can both report the same task.
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    first = db.record_checklist_item(conn, "sess-1", "1", "Write tests")

    second = db.record_checklist_item(conn, "sess-1", "1", "Write more tests")

    session_id = db.list_sessions(conn)[0]["id"]
    items = db.list_checklist_items(conn, session_id)
    assert second == first
    assert len(items) == 1
    assert items[0]["title"] == "Write more tests"


def test_set_checklist_item_status_tracks_in_progress_and_completed(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.record_checklist_item(conn, "sess-1", "1", "Write tests")
    session_id = db.list_sessions(conn)[0]["id"]

    assert db.set_checklist_item_status(conn, "sess-1", "1", "in_progress") is True
    assert db.list_checklist_items(conn, session_id)[0]["status"] == "in_progress"

    assert db.set_checklist_item_status(conn, "sess-1", "1", "completed") is True
    item = db.list_checklist_items(conn, session_id)[0]
    assert item["status"] == "done"
    assert item["completed_by"] == "agent"
    assert item["completed_at"] is not None


def test_set_checklist_item_status_deleted_removes_item(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.record_checklist_item(conn, "sess-1", "1", "Write tests")

    assert db.set_checklist_item_status(conn, "sess-1", "1", "deleted") is True

    assert db.list_checklist_items(conn, db.list_sessions(conn)[0]["id"]) == []


def test_set_checklist_item_status_unknown_task_or_session_is_noop(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    assert db.set_checklist_item_status(conn, "sess-1", "nope", "completed") is False
    assert db.set_checklist_item_status(conn, "sess-ghost", "1", "completed") is False


def test_sync_todo_list_mirrors_the_full_list(db_path, tmp_path):
    # TodoWrite sends the whole list every time; dropped todos disappear, manual items stay.
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    session_id = db.list_sessions(conn)[0]["id"]
    db.add_checklist_item(conn, session_id, "Manual step")

    db.sync_todo_list(conn, "sess-1", [
        {"content": "Read code", "status": "completed"},
        {"content": "Write fix", "status": "in_progress"},
        {"content": "Ship", "status": "pending"},
    ])
    db.sync_todo_list(conn, "sess-1", [
        {"content": "Read code", "status": "completed"},
        {"content": "Write fix", "status": "completed"},
    ])

    items = {i["title"]: i for i in db.list_checklist_items(conn, session_id)}
    assert set(items) == {"Manual step", "Read code", "Write fix"}
    assert items["Write fix"]["status"] == "done"
    assert items["Read code"]["status"] == "done"
    assert items["Manual step"]["status"] == "pending"


def test_sync_todo_list_noop_for_unknown_session(db_path):
    conn = db.get_connection()
    db.sync_todo_list(conn, "sess-ghost", [{"content": "x", "status": "pending"}])  # must not raise


# --- Last-done summary ---


def test_record_session_summary_exposed_by_list_sessions(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    db.record_session_summary(
        conn, "sess-1", "Fixed the login bug and added tests.",
        title="Login bug fix", last_prompt="why does login fail?",
    )

    s = db.list_sessions(conn)[0]
    assert s["last_summary"] == "Fixed the login bug and added tests."
    assert s["title"] == "Login bug fix"
    assert s["last_prompt"] == "why does login fail?"
    assert s["last_summary_at"] is not None


def test_record_session_summary_keeps_previous_values_when_new_ones_missing(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.record_session_summary(conn, "sess-1", "First turn.", title="Title", last_prompt="Prompt")

    db.record_session_summary(conn, "sess-1", None, title=None, last_prompt=None)

    s = db.list_sessions(conn)[0]
    assert (s["last_summary"], s["title"], s["last_prompt"]) == ("First turn.", "Title", "Prompt")


def test_record_session_summary_truncates_very_long_text(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    db.record_session_summary(conn, "sess-1", "x" * 50_000)

    assert len(db.list_sessions(conn)[0]["last_summary"]) <= db.SUMMARY_MAX_CHARS + 1


def test_record_session_summary_noop_for_unknown_session(db_path):
    conn = db.get_connection()
    db.record_session_summary(conn, "sess-ghost", "text")  # must not raise


def test_record_session_summary_strips_markdown_and_collapses_to_one_line(db_path, tmp_path):
    """The recap is meant to be a quick memory-jog shown clamped to a couple of
    lines on a fleet card — Claude's actual final message is often a full
    markdown-formatted, multi-paragraph wrap-up, and displaying that verbatim
    (bold/code markers, fenced code blocks, blank lines between paragraphs all
    included) reads as noise rather than a recap, even once visually clamped."""
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    raw = (
        "Fixed the **token refresh** bug.\n\n"
        "Also ran:\n```python\ndef foo():\n    pass\n```\n\n"
        "See `auth.py` for details.\n\n"
        "1. Updated tests\n2. Shipped it"
    )
    db.record_session_summary(conn, "sess-1", raw)

    cleaned = db.list_sessions(conn)[0]["last_summary"]
    assert "**" not in cleaned
    assert "```" not in cleaned
    assert "`" not in cleaned
    assert "def foo" not in cleaned  # fenced code block content is dropped, not just unfenced
    assert "\n" not in cleaned
    assert "Fixed the token refresh bug." in cleaned
    assert "See auth.py for details." in cleaned


def test_record_session_summary_does_not_touch_title_or_prompt_markdown(db_path, tmp_path):
    """Only the agent's own wrap-up (last_summary) gets cleaned — title is Claude
    Code's own short auto-title, and last_prompt is the human's own verbatim
    words, neither of which should be silently rewritten."""
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    db.record_session_summary(
        conn, "sess-1", "plain summary", title="Fix `foo()` **fast**", last_prompt="please fix `foo()`",
    )

    s = db.list_sessions(conn)[0]
    assert s["title"] == "Fix `foo()` **fast**"
    assert s["last_prompt"] == "please fix `foo()`"
