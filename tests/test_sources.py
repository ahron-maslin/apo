from apo import db


def _configure_linear(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")
    monkeypatch.setenv("LINEAR_TEAM", "ENG")


def test_linear_source_unconfigured_by_default(sources_module):
    assert sources_module.LinearSource().is_configured() is False


def test_create_decision_request_pushes_to_linear_when_configured(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
    })

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    decision = db.get_decision_request(conn, decision_id)
    assert decision["linear_issue_id"] == "iss-1"
    assert decision["linear_identifier"] == "ENG-1"
    assert decision["linear_push_error"] is None


def test_create_decision_request_pushes_to_linear_with_plain_string_options(db_path, config_module, sources_module, monkeypatch):
    """Regression test: every real caller files `options` as a plain list of
    strings, not {label, detail} dicts, and linear_client.create_issue used to
    crash on that shape (`'str' object has no attribute 'get'`), silently dropping
    the Linear push and recording a sync error instead of creating the issue."""
    _configure_linear(monkeypatch)
    captured = {}

    def fake_create_issue(project_name, decision):
        captured["decision"] = decision
        return {"id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1"}

    monkeypatch.setattr(sources_module.linear_client, "create_issue", fake_create_issue)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?",
        options=["Bottom tabs", "Drawer"],
    )

    decision = db.get_decision_request(conn, decision_id)
    assert decision["linear_issue_id"] == "iss-1"
    assert decision["linear_push_error"] is None
    assert captured["decision"]["options"] == [
        {"label": "Bottom tabs", "detail": None, "consequence": None, "reversible": None},
        {"label": "Drawer", "detail": None, "consequence": None, "reversible": None},
    ]


def test_push_failure_recorded_without_raising(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)

    def boom(project_name, decision):
        raise RuntimeError("network is down")

    monkeypatch.setattr(sources_module.linear_client, "create_issue", boom)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    decision = db.get_decision_request(conn, decision_id)
    assert decision["linear_issue_id"] is None
    assert "network is down" in decision["linear_push_error"]
    errors = db.recent_sync_errors(conn)
    assert errors[0]["source"] == "linear_push"


def test_poll_new_answers_advances_synced_comment_cursor(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
    })

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    comments = [{"id": "c1", "body": "First reply", "user": {"name": "Alice"}}]
    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: comments)

    src = sources_module.LinearSource()
    first_poll = src.poll_new_answers()
    assert len(first_poll) == 1
    assert first_poll[0].decision_id == decision_id
    assert "First reply" in first_poll[0].answer_text

    # Same comment set, no new comments since — should return nothing.
    second_poll = src.poll_new_answers()
    assert second_poll == []

    # A new comment arrives.
    comments.append({"id": "c2", "body": "Follow-up", "user": {"name": "Alice"}})
    third_poll = src.poll_new_answers()
    assert len(third_poll) == 1
    assert "Follow-up" in third_poll[0].answer_text
    assert "First reply" not in third_poll[0].answer_text


def test_poll_new_answers_skips_decisions_already_answered(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
    })
    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: [
        {"id": "c1", "body": "reply", "user": {"name": "Alice"}}
    ])
    # answer_decision_request below triggers mark_resolved -> move_to_state; mock it
    # too, or this makes a real call to the live Linear API (config_module.LINEAR_API_KEY
    # is truthy here even though it's a fake value — is_configured() only checks
    # truthiness, so an unmocked network call still fires and only fails on a real
    # 401, not on being skipped).
    monkeypatch.setattr(sources_module.linear_client, "move_to_state", lambda issue_id, type_or_name: None)
    monkeypatch.setattr(sources_module.linear_client, "create_comment", lambda issue_id, body: None)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    db.answer_decision_request(conn, decision_id, "Answered via dashboard already")

    assert sources_module.LinearSource().poll_new_answers() == []


def test_poll_skips_decisions_finished_long_ago(db_path, config_module, sources_module, monkeypatch):
    """It polled every decision that ever had an issue, every 30s, forever. Follow-up
    comments matter for a while after an answer, not for months."""
    _configure_linear(monkeypatch)
    polled = []
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "pending", "identifier": "ENG-0", "url": "https://linear.app/x/ENG-0",
    })
    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: polled.append(issue_id) or [])

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    rows = {
        "open": ("open", None),
        "answered-old": ("answered", "2026-01-01T00:00:00+00:00"),
        "resolved-recent": ("resolved", db._now()),
        "resolved-old": ("resolved", "2026-01-01T00:00:00+00:00"),
    }
    for issue_id, (status, answered_at) in rows.items():
        decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
        conn.execute(
            "UPDATE decision_requests SET linear_issue_id = ?, status = ?, answered_at = ? WHERE id = ?",
            (issue_id, status, answered_at, decision_id),
        )
    conn.commit()

    sources_module.LinearSource().poll_new_answers()

    assert sorted(polled) == ["answered-old", "open", "resolved-recent"]


def test_poll_new_answers_flags_comment_after_answer_as_sync_error(db_path, config_module, sources_module, monkeypatch):
    # Regression test: a human replying on Linear *after* a decision was already
    # answered (e.g. correcting a bad first answer) used to be silently dropped
    # forever, because the old query excluded any row with answer_text set. That's
    # exactly what happened with CLA-95 — a non-answer auto-resolved the decision
    # and a follow-up would never have been seen.
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
    })
    monkeypatch.setattr(sources_module.linear_client, "move_to_state", lambda issue_id, type_or_name: None)
    monkeypatch.setattr(sources_module.linear_client, "create_comment", lambda issue_id, body: None)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    db.answer_decision_request(conn, decision_id, "premature non-answer")

    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: [
        {"id": "c1", "body": "actually, wait — here's the real answer", "user": {"name": "Alice"}}
    ])

    assert sources_module.LinearSource().poll_new_answers() == []

    errors = db.recent_sync_errors(conn)
    assert errors[0]["source"] == "linear_post_answer_comment"
    assert errors[0]["decision_request_id"] == decision_id
    assert "real answer" in errors[0]["message"]

    decision = db.get_decision_request(conn, decision_id)
    assert decision["linear_synced_comment_id"] == "c1"

    # Cursor advanced, so the same comment isn't flagged again on the next poll.
    assert sources_module.LinearSource().poll_new_answers() == []
    assert len(db.recent_sync_errors(conn, limit=10)) == 1


def test_create_decision_request_does_not_push_when_connector_disabled(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    calls = []
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: calls.append(1) or {"id": "x", "identifier": "x", "url": "x"})

    conn = db.get_connection()
    db.set_connector_enabled(conn, "linear", False)
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    assert calls == []
    decision = db.get_decision_request(conn, decision_id)
    assert decision["linear_issue_id"] is None


def test_mark_resolved_skipped_when_connector_disabled(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
    })

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    calls = []
    monkeypatch.setattr(sources_module.linear_client, "create_comment", lambda issue_id, body: calls.append(("comment", issue_id, body)))
    monkeypatch.setattr(sources_module.linear_client, "move_to_state", lambda issue_id, type_or_name: calls.append(("state", issue_id, type_or_name)))
    db.set_connector_enabled(conn, "linear", False)

    db.answer_decision_request(conn, decision_id, "Use drawer navigation.")

    assert calls == []


def test_retry_pending_succeeds_and_clears_error(db_path, config_module, sources_module, monkeypatch):
    """The queue is just the DB rows with a recorded push failure and no issue yet —
    retrying is exactly push_question again, which already knows how to succeed-and-
    clear or fail-and-re-record."""
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    conn.execute(
        "UPDATE decision_requests SET linear_push_error = 'connection reset' WHERE id = ?", (decision_id,)
    )
    conn.commit()

    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
    })

    _make_retries_due(conn)
    sources_module.LinearSource().retry_pending()

    decision = db.get_decision_request(conn, decision_id)
    assert decision["linear_issue_id"] == "iss-1"
    assert decision["linear_push_error"] is None


def test_retry_pending_skips_decisions_already_pushed(db_path, config_module, sources_module, monkeypatch):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    conn.execute(
        "UPDATE decision_requests SET linear_issue_id = 'already-there' WHERE id = ?", (decision_id,)
    )
    conn.commit()

    _configure_linear(monkeypatch)
    calls = []
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: calls.append(1))

    _make_retries_due(conn)
    sources_module.LinearSource().retry_pending()

    assert calls == []


def test_retry_pending_leaves_the_error_if_still_failing(db_path, config_module, sources_module, monkeypatch):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    conn.execute(
        "UPDATE decision_requests SET linear_push_error = 'connection reset' WHERE id = ?", (decision_id,)
    )
    conn.commit()

    _configure_linear(monkeypatch)

    def still_failing(project_name, decision):
        raise RuntimeError("still down")

    monkeypatch.setattr(sources_module.linear_client, "create_issue", still_failing)

    _make_retries_due(conn)
    sources_module.LinearSource().retry_pending()

    decision = db.get_decision_request(conn, decision_id)
    assert decision["linear_issue_id"] is None
    assert decision["linear_push_error"] == "still down"


def test_mark_resolved_posts_answer_comment_before_closing(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
    })

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    calls = []
    monkeypatch.setattr(sources_module.linear_client, "create_comment", lambda issue_id, body: calls.append(("comment", issue_id, body)))
    monkeypatch.setattr(sources_module.linear_client, "move_to_state", lambda issue_id, type_or_name: calls.append(("state", issue_id, type_or_name)))

    db.answer_decision_request(conn, decision_id, "Use drawer navigation.", source="dashboard")

    assert calls[0][0] == "comment"
    assert calls[0][1] == "iss-1"
    assert "Use drawer navigation." in calls[0][2]
    assert calls[1] == ("state", "iss-1", "completed")


def test_mark_resolved_skips_comment_when_answer_came_from_linear_itself(db_path, config_module, sources_module, monkeypatch):
    # The answer text is already visible in the issue as the comment that produced
    # it — posting it back would just echo the human's own comment.
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
    })

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    comment_calls = []
    monkeypatch.setattr(sources_module.linear_client, "create_comment", lambda issue_id, body: comment_calls.append((issue_id, body)))
    monkeypatch.setattr(sources_module.linear_client, "move_to_state", lambda issue_id, type_or_name: None)

    db.answer_decision_request(conn, decision_id, "Answered on Linear itself", source="linear")

    assert comment_calls == []


def _backdate_sync_errors(conn, hours=1):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn.execute("UPDATE sync_errors SET occurred_at = ?", (old,))
    conn.commit()


def _unresolved(conn):
    return db.recent_sync_errors(conn, limit=100, unresolved_only=True)


def _make_retries_due(conn):
    conn.execute("UPDATE decision_requests SET linear_next_retry_at = NULL")
    conn.commit()


def _failing_push_decision(conn, sources_module, monkeypatch):
    calls = []

    def push_fails(project_name, decision):
        calls.append(1)
        raise RuntimeError("push broke")

    monkeypatch.setattr(sources_module.linear_client, "create_issue", push_fails)
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    return decision_id, calls


def test_failed_push_is_not_retried_before_its_backoff_elapses(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    conn = db.get_connection()
    _, calls = _failing_push_decision(conn, sources_module, monkeypatch)

    sources_module.LinearSource().retry_pending()
    assert len(calls) == 1

    _make_retries_due(conn)
    sources_module.LinearSource().retry_pending()
    assert len(calls) == 2


def test_failed_push_gives_up_after_five_attempts(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    conn = db.get_connection()
    decision_id, calls = _failing_push_decision(conn, sources_module, monkeypatch)

    for _ in range(10):
        _make_retries_due(conn)
        sources_module.LinearSource().retry_pending()

    assert len(calls) == 5
    decision = db.get_decision_request(conn, decision_id)
    assert decision["linear_issue_id"] is None
    assert decision["linear_retry_attempts"] == 5
    assert "gave up after 5 attempts" in _unresolved(conn)[0]["message"]


def test_failed_resolve_gives_up_after_five_attempts(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "u",
    })
    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: [])
    monkeypatch.setattr(sources_module.linear_client, "create_comment", lambda issue_id, body: None)
    calls = []

    def state_fails(issue_id, type_or_name):
        calls.append(1)
        raise TimeoutError("timed out")

    monkeypatch.setattr(sources_module.linear_client, "move_to_state", state_fails)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    db.answer_decision_request(conn, decision_id, "Go with A")

    for _ in range(10):
        _make_retries_due(conn)
        sources_module.LinearSource().retry_pending()

    assert len(calls) == 5
    assert db.get_decision_request(conn, decision_id)["linear_resolve_error"]


def test_successful_push_gives_the_resolve_a_fresh_set_of_attempts(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    conn = db.get_connection()
    decision_id, _ = _failing_push_decision(conn, sources_module, monkeypatch)
    for _ in range(3):
        _make_retries_due(conn)
        sources_module.LinearSource().retry_pending()

    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "u",
    })
    _make_retries_due(conn)
    sources_module.LinearSource().retry_pending()

    decision = db.get_decision_request(conn, decision_id)
    assert decision["linear_issue_id"] == "iss-1"
    assert decision["linear_retry_attempts"] == 0
    assert decision["linear_next_retry_at"] is None


def test_successful_poll_resolves_earlier_poll_errors(db_path, config_module, sources_module, monkeypatch):
    """Intermittent read timeouts used to keep the dashboard banner permanently lit —
    once Linear answers again, the earlier failures are no longer a current problem."""
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "u",
    })
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    def timeout(issue_id):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(sources_module.linear_client, "get_comments", timeout)
    src = sources_module.LinearSource()
    src.poll_new_answers()
    assert [e["source"] for e in _unresolved(conn)] == ["linear_poll"]
    _backdate_sync_errors(conn)

    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: [])
    src.poll_new_answers()

    assert _unresolved(conn) == []
    history = db.recent_sync_errors(conn, limit=100)
    assert len(history) == 1 and history[0]["resolved_at"] is not None


def test_poll_error_after_the_success_stays_unresolved(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    issues = iter(["iss-1", "iss-2"])
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": next(issues), "identifier": "ENG", "url": "u",
    })
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_decision_request(conn, project_id=project["id"], title="A", question="Q?")
    db.create_decision_request(conn, project_id=project["id"], title="B", question="Q?")

    def flaky(issue_id):
        if issue_id == "iss-2":
            raise TimeoutError("timed out")
        return []

    monkeypatch.setattr(sources_module.linear_client, "get_comments", flaky)
    sources_module.LinearSource().poll_new_answers()

    assert [e["source"] for e in _unresolved(conn)] == ["linear_poll"]


def test_successful_sync_leaves_push_errors_for_decisions_still_unpushed(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)

    def push_fails(project_name, decision):
        raise RuntimeError("push broke")

    monkeypatch.setattr(sources_module.linear_client, "create_issue", push_fails)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_decision_request(conn, project_id=project["id"], title="Unpushed", question="Q?")
    _backdate_sync_errors(conn)

    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-2", "identifier": "ENG-2", "url": "u",
    })
    db.create_decision_request(conn, project_id=project["id"], title="Pushed", question="Q?")
    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: [])
    sources_module.LinearSource().poll_new_answers()

    assert [e["source"] for e in _unresolved(conn)] == ["linear_push"]


def test_retry_pending_repushes_and_resolves_the_push_error(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)

    def push_fails(project_name, decision):
        raise RuntimeError("network is down")

    monkeypatch.setattr(sources_module.linear_client, "create_issue", push_fails)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    _backdate_sync_errors(conn)

    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "u",
    })
    _make_retries_due(conn)
    sources_module.LinearSource().retry_pending()

    assert db.get_decision_request(conn, decision_id)["linear_issue_id"] == "iss-1"
    assert _unresolved(conn) == []


def test_failed_resolve_is_retried_without_reposting_the_comment(db_path, config_module, sources_module, monkeypatch):
    """mark_resolved used to be fire-once: a Linear blip while answering left the issue
    open in Linear forever, with nothing recording that it still needed closing."""
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "u",
    })
    posted = []
    calls = []

    def comment(issue_id, body):
        posted.append({"id": f"c{len(posted)}", "body": body, "user": {"name": "Me"}})

    def state_fails(issue_id, type_or_name):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(sources_module.linear_client, "create_comment", comment)
    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: list(posted))
    monkeypatch.setattr(sources_module.linear_client, "move_to_state", state_fails)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    db.answer_decision_request(conn, decision_id, "Go with A", source="dashboard")

    assert db.get_decision_request(conn, decision_id)["linear_resolve_error"]
    assert [e["source"] for e in _unresolved(conn)] == ["linear_resolve"]
    _backdate_sync_errors(conn)

    monkeypatch.setattr(sources_module.linear_client, "move_to_state", lambda issue_id, type_or_name: calls.append((issue_id, type_or_name)))
    _make_retries_due(conn)
    sources_module.LinearSource().retry_pending()

    assert calls == [("iss-1", "completed")]
    assert len(posted) == 1
    assert db.get_decision_request(conn, decision_id)["linear_resolve_error"] is None
    assert _unresolved(conn) == []


def test_failed_resolve_retry_posts_the_comment_if_it_never_landed(db_path, config_module, sources_module, monkeypatch):
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "u",
    })

    def comment_fails(issue_id, body):
        raise TimeoutError("timed out")

    monkeypatch.setattr(sources_module.linear_client, "create_comment", comment_fails)
    monkeypatch.setattr(sources_module.linear_client, "move_to_state", lambda issue_id, type_or_name: None)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    db.answer_decision_request(conn, decision_id, "Go with A", source="dashboard")
    assert db.get_decision_request(conn, decision_id)["linear_resolve_error"]

    posted = []
    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: [])
    monkeypatch.setattr(sources_module.linear_client, "create_comment", lambda issue_id, body: posted.append(body))
    _make_retries_due(conn)
    sources_module.LinearSource().retry_pending()

    assert len(posted) == 1 and "Go with A" in posted[0]
    assert db.get_decision_request(conn, decision_id)["linear_resolve_error"] is None


def test_poll_ignores_our_own_answered_via_comment(db_path, config_module, sources_module, monkeypatch):
    """mark_resolved posts "**Answered via dashboard:**" onto the issue; reading that
    back as a human reply after the answer flagged a bogus post-answer sync error."""
    _configure_linear(monkeypatch)
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "u",
    })
    monkeypatch.setattr(sources_module.linear_client, "move_to_state", lambda issue_id, type_or_name: None)
    monkeypatch.setattr(sources_module.linear_client, "create_comment", lambda issue_id, body: None)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    db.answer_decision_request(conn, decision_id, "Go with A")

    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: [
        {"id": "c1", "body": "**Answered via dashboard:**\n\nGo with A", "user": {"name": "Me"}}
    ])
    assert sources_module.LinearSource().poll_new_answers() == []
    assert db.recent_sync_errors(conn, limit=100) == []
    assert db.get_decision_request(conn, decision_id)["linear_synced_comment_id"] == "c1"
