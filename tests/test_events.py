"""One append-only event log: what happened, to what, by whom, in order. It drives the
Activity page and "while you were away". Only state changes write events — never
heartbeats or plan churn — so it stays readable."""
from apo import db


def _verbs(conn, **filters):
    return [e["verb"] for e in db.list_events(conn, oldest_first=True, **filters)]


def test_a_decisions_lifecycle_is_recorded_in_order(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Postgres or SQLite?", question="Q?")
    db.answer_decision_request(conn, decision_id, "Postgres")
    db.resolve_decision_request(conn, decision_id)

    events = db.list_events(conn, decision_id=decision_id, oldest_first=True)
    assert [e["verb"] for e in events] == ["item.created", "item.answered", "item.applied"]
    assert events[0]["summary"] == "Decision #1 filed: Postgres or SQLite?"
    assert events[1]["actor"] == "human" and events[1]["source"] == "dashboard"
    assert "Postgres" in events[1]["summary"]
    assert all(e["project_id"] == project["id"] for e in events)
    assert events[0]["seq"] < events[1]["seq"] < events[2]["seq"]


def test_actions_and_archive_closures_are_recorded(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    action_id = db.create_action_request(conn, project_id=project["id"], title="Add DNS record", question="Q?")
    db.archive_project(conn, project["id"])

    events = db.list_events(conn, decision_id=action_id, oldest_first=True)
    assert events[0]["summary"] == "Action #1 filed: Add DNS record"
    assert events[-1]["verb"] == "item.closed"


def test_session_lifecycle_records_state_changes_but_not_heartbeats(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    for _ in range(5):
        db.session_heartbeat(conn, "sess-1")
    db.session_waiting(conn, "sess-1", "permission", "Bash: make")
    db.session_waiting(conn, "sess-1", "permission", "Bash: make")
    db.session_heartbeat(conn, "sess-1")
    db.session_stop(conn, "sess-1")
    db.session_stop_failure(conn, "sess-1", "overloaded")
    db.session_end(conn, "sess-1", "process_gone_mid_turn")

    assert _verbs(conn, session_id="sess-1", min_importance=0) == [
        "session.started", "session.waiting", "session.turn_ended", "session.errored", "session.crashed",
    ]


def test_a_resumed_session_is_recorded_once(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_end(conn, "sess-1", "logout")
    db.register_session(conn, "sess-1", str(tmp_path))

    assert _verbs(conn, session_id="sess-1") == ["session.started", "session.ended", "session.resumed"]


def test_routine_turn_ends_are_trace_level(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_stop(conn, "sess-1")

    assert "session.turn_ended" not in _verbs(conn)
    assert "session.turn_ended" in _verbs(conn, min_importance=0)


def test_events_since_a_cursor_and_per_project(db_path):
    conn = db.get_connection()
    acme = db.get_or_create_project(conn, "acme")
    globex = db.get_or_create_project(conn, "globex")
    db.create_decision_request(conn, project_id=acme["id"], title="A", question="Q?")
    cursor = db.latest_event_seq(conn)
    db.create_decision_request(conn, project_id=globex["id"], title="G", question="Q?")
    db.create_decision_request(conn, project_id=acme["id"], title="A2", question="Q?")

    assert [e["summary"] for e in db.list_events(conn, since_seq=cursor)] == [
        "Decision #3 filed: A2", "Decision #2 filed: G",
    ]
    assert len(db.list_events(conn, project_id=acme["id"])) == 2


def test_ui_state_round_trips(db_path):
    conn = db.get_connection()
    assert db.get_ui_state(conn, "last_seen_seq") is None
    db.set_ui_state(conn, "last_seen_seq", "42")
    assert db.get_ui_state(conn, "last_seen_seq") == "42"


def test_upgrading_backfills_events_from_history(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Old one", question="Q?")
    db.answer_decision_request(conn, decision_id, "yes")
    db.register_session(conn, "sess-1", str(tmp_path))
    conn.execute("DELETE FROM events")
    conn.execute("PRAGMA user_version = 6")  # the version before events existed
    conn.commit()

    conn = db.get_connection()

    verbs = _verbs(conn, min_importance=0)
    assert "item.created" in verbs and "item.answered" in verbs and "session.started" in verbs
    assert db.list_events(conn, decision_id=decision_id, oldest_first=True)[0]["summary"] == "Decision #1 filed: Old one"
