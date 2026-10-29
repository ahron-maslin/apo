"""What a session is actually doing, from free signals: Claude Code's hooks while it
runs, and `claude agents --json` (via reconcile_live_sessions) while the dashboard is
being looked at. The point is that the states that need a human — a permission
prompt, a question, an API error — are visible, and the ones that don't — an idle
session sitting at its prompt — never look like an alarm."""
from datetime import datetime, timedelta, timezone

from apo import db


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _session(conn, sid="sess-1"):
    return {s["claude_session_id"]: s for s in db.list_sessions(conn, include_ended=True)}[sid]


def test_a_tool_call_means_working(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_heartbeat(conn, "sess-1", str(tmp_path))

    s = _session(conn)
    assert s["exec_state"] == "working"
    assert s["display_status"] == "running"
    assert s["needs_you"] is False


def test_a_permission_request_needs_you_and_says_for_what(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_heartbeat(conn, "sess-1")

    db.session_waiting(conn, "sess-1", "permission", "Bash: stripe listen --forward-to localhost")

    s = _session(conn)
    assert s["display_status"] == "waiting_permission"
    assert s["waiting_detail"] == "Bash: stripe listen --forward-to localhost"
    assert s["needs_you"] is True


def test_waiting_for_you_is_never_flagged_as_stale(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_waiting(conn, "sess-1", "permission", "Bash: rm -rf build")
    conn.execute("UPDATE sessions SET last_seen_at = ?", (_ago(days=7),))
    conn.commit()

    s = _session(conn)
    assert s["display_status"] == "waiting_permission"
    assert s["stale"] is False


def test_an_input_request_needs_you(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_waiting(conn, "sess-1", "input", "Which database should the queue use?")

    s = _session(conn)
    assert s["display_status"] == "waiting_input"
    assert s["needs_you"] is True


def test_the_next_tool_call_clears_a_permission_wait(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_waiting(conn, "sess-1", "permission", "Bash: make")

    db.session_heartbeat(conn, "sess-1")

    s = _session(conn)
    assert s["display_status"] == "running"
    assert s["waiting_detail"] is None


def test_stop_puts_the_session_at_rest_and_clears_any_wait(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_waiting(conn, "sess-1", "input", "?")

    db.session_stop(conn, "sess-1")

    s = _session(conn)
    assert s["exec_state"] == "at_rest"
    assert s["display_status"] == "idle"
    assert s["needs_you"] is False


def test_a_late_async_heartbeat_cannot_undo_the_turn_ending(db_path, tmp_path):
    """The heartbeat runs async, so the process for the turn's last tool call can
    finish after that turn's (synchronous) Stop hook. Its event happened first."""
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    event_at = db._now()
    db.session_stop(conn, "sess-1")

    db.session_heartbeat(conn, "sess-1", event_at=event_at)
    db.session_waiting(conn, "sess-1", "permission", "Bash: late", event_at=event_at)

    s = _session(conn)
    assert s["display_status"] == "idle"
    assert s["waiting_detail"] is None


def test_an_api_error_ending_the_turn_needs_you(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_heartbeat(conn, "sess-1")

    db.session_stop_failure(conn, "sess-1", "overloaded")

    s = _session(conn)
    assert s["display_status"] == "errored"
    assert s["last_error"] == "overloaded"
    assert s["needs_you"] is True


def test_a_new_prompt_starts_a_turn_and_clears_the_error(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_stop_failure(conn, "sess-1", "overloaded")

    db.session_prompt_submitted(conn, "sess-1", "try again please", str(tmp_path))

    s = _session(conn)
    assert s["display_status"] == "running"
    assert s["last_error"] is None
    assert s["last_prompt"] == "try again please"


def test_a_late_async_prompt_hook_cannot_undo_the_turn_ending(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    event_at = db._now()
    db.session_stop(conn, "sess-1")

    db.session_prompt_submitted(conn, "sess-1", "quick question", str(tmp_path), event_at=event_at)

    assert _session(conn)["display_status"] == "idle"


def test_prompt_submitted_self_heals_an_unknown_session(db_path, tmp_path):
    conn = db.get_connection()
    db.session_prompt_submitted(conn, "sess-new", "hello", str(tmp_path))
    assert _session(conn, "sess-new")["exec_state"] == "working"


def test_an_idle_session_is_never_stale_however_long_it_waits(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_heartbeat(conn, "sess-1")
    db.session_stop(conn, "sess-1")
    conn.execute("UPDATE sessions SET last_seen_at = ?", (_ago(days=3),))
    conn.commit()

    assert _session(conn)["stale"] is False


def test_a_working_session_that_has_gone_quiet_is_flagged_quiet(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_heartbeat(conn, "sess-1")
    conn.execute("UPDATE sessions SET last_seen_at = ?", (_ago(minutes=db.STALE_MINUTES + 13),))
    conn.commit()

    s = _session(conn)
    assert s["stale"] is True
    assert s["quiet_minutes"] == db.STALE_MINUTES + 13


def test_a_session_that_never_took_a_turn_is_a_ghost(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "ghost", str(tmp_path))
    db.register_session(conn, "fresh", str(tmp_path))
    db.register_session(conn, "used", str(tmp_path))
    db.session_prompt_submitted(conn, "used", "hi", str(tmp_path))
    conn.execute(
        "UPDATE sessions SET started_at = ?, last_seen_at = ? WHERE claude_session_id IN ('ghost', 'used')",
        (_ago(hours=3), _ago(hours=3)),
    )
    conn.commit()

    by_id = {s["claude_session_id"]: s for s in db.list_sessions(conn)}
    assert by_id["ghost"]["ghost"] is True
    assert by_id["fresh"]["ghost"] is False
    assert by_id["used"]["ghost"] is False


def test_a_live_session_found_by_reconcile_is_not_a_ghost(db_path, tmp_path):
    """Sessions opened before the hooks were installed have no hook activity at all,
    but Claude Code itself says they're alive — they belong on the fleet."""
    conn = db.get_connection()
    db.reconcile_live_sessions(conn, [{"pid": 7, "sessionId": "old-terminal", "cwd": str(tmp_path), "status": "idle"}])
    conn.execute("UPDATE sessions SET started_at = ?, last_seen_at = ?", (_ago(days=9), _ago(days=9)))
    conn.commit()

    assert _session(conn, "old-terminal")["ghost"] is False


def test_a_session_waiting_on_you_is_never_hidden_as_a_ghost(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    conn.execute(
        "UPDATE sessions SET exec_state = 'waiting_permission', started_at = ?, last_seen_at = ?",
        (_ago(hours=3), _ago(hours=3)),
    )
    conn.commit()

    assert _session(conn)["ghost"] is False


# --- reconcile_live_sessions -------------------------------------------------


def test_reconcile_shows_a_permission_prompt_the_hooks_missed(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_stop(conn, "sess-1")

    db.reconcile_live_sessions(conn, [
        {"pid": 42, "sessionId": "sess-1", "cwd": str(tmp_path), "status": "waiting", "waitingFor": "permission prompt"},
    ])

    s = _session(conn)
    assert s["display_status"] == "waiting_permission"
    assert s["pid"] == 42


def test_reconcile_maps_other_waits_to_input(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.reconcile_live_sessions(conn, [
        {"pid": 42, "sessionId": "sess-1", "cwd": str(tmp_path), "status": "waiting", "waitingFor": "dialog open"},
    ])
    assert _session(conn)["display_status"] == "waiting_input"


def test_reconcile_clears_a_wait_once_the_session_is_busy_again(db_path, tmp_path):
    """After the person approves a prompt in the terminal, nothing fires until the
    tool finishes — a long tool would otherwise keep showing 'needs permission'."""
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_waiting(conn, "sess-1", "permission", "Bash: npm install")

    db.reconcile_live_sessions(conn, [{"pid": 42, "sessionId": "sess-1", "status": "busy"}])

    assert _session(conn)["display_status"] == "running"


def test_reconcile_settles_a_session_whose_stop_hook_was_missed(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_heartbeat(conn, "sess-1")

    db.reconcile_live_sessions(conn, [{"pid": 42, "sessionId": "sess-1", "status": "idle"}])

    s = _session(conn)
    assert s["exec_state"] == "at_rest"
    assert s["display_status"] == "idle"


def test_reconcile_registers_a_live_session_it_never_saw(db_path, tmp_path):
    conn = db.get_connection()
    db.reconcile_live_sessions(conn, [
        {"pid": 7, "sessionId": "never-seen", "cwd": str(tmp_path / "proj"), "status": "waiting",
         "waitingFor": "permission prompt"},
    ])

    s = _session(conn, "never-seen")
    assert s["display_status"] == "waiting_permission"
    assert s["project_name"] == "proj"


def test_reconcile_ends_sessions_whose_process_is_gone(db_path, tmp_path):
    conn = db.get_connection()
    for sid in ("at-rest", "mid-turn", "just-started"):
        db.register_session(conn, sid, str(tmp_path))
    db.session_stop(conn, "at-rest")
    db.session_heartbeat(conn, "mid-turn")
    conn.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE claude_session_id IN ('at-rest', 'mid-turn')",
        (_ago(minutes=30),),
    )
    conn.commit()

    db.reconcile_live_sessions(conn, [])

    assert _session(conn, "at-rest")["status"] == "ended"
    assert _session(conn, "at-rest")["end_reason"] == "process_gone"
    assert _session(conn, "mid-turn")["display_status"] == "crashed"
    # Seen moments ago: not yet in `claude agents` is a race, not a disappearance.
    assert _session(conn, "just-started")["status"] == "running"


def test_reconcile_prefers_the_entry_with_a_live_status_when_a_session_is_listed_twice(db_path, tmp_path):
    """Live `claude agents --json` lists some sessions twice: once without a status
    (the job record) and once with it (the process)."""
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_stop(conn, "sess-1")
    entries = [
        {"pid": 5, "sessionId": "sess-1", "cwd": str(tmp_path), "status": "waiting", "waitingFor": "permission prompt"},
        {"sessionId": "sess-1", "cwd": str(tmp_path)},
    ]

    db.reconcile_live_sessions(conn, entries)

    assert _session(conn)["display_status"] == "waiting_permission"


def test_reconcile_with_no_information_changes_nothing(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    conn.execute("UPDATE sessions SET last_seen_at = ?", (_ago(hours=5),))
    conn.commit()

    db.reconcile_live_sessions(conn, None)

    assert _session(conn)["status"] == "running"


def test_reconcile_revives_a_session_that_comes_back(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_end(conn, "sess-1", "process_gone")

    db.reconcile_live_sessions(conn, [{"pid": 9, "sessionId": "sess-1", "cwd": str(tmp_path), "status": "idle"}])

    s = _session(conn)
    assert s["status"] != "ended"
    assert s["exec_state"] == "at_rest"


def test_reconcile_keeps_blocked_sessions_blocked_when_idle(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="sess-1",
    )

    db.reconcile_live_sessions(conn, [{"pid": 9, "sessionId": "sess-1", "status": "idle"}])

    assert _session(conn)["display_status"] == "blocked_decision"
