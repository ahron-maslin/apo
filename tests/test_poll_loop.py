from apo import db


async def test_poll_sources_once_answers_and_writes_into_the_repo(
    db_path, tmp_path, config_module, sources_module, monkeypatch,
):
    """The full Linear -> repo path, which is the one that has to work without any
    session running: a comment on the issue becomes an ANSWERED row in the
    repository's own INDEX.md, with nothing spawned and nothing to monitor."""
    monkeypatch.setenv("APO_LINEAR_API_KEY", "fake-key")
    monkeypatch.setenv("APO_LINEAR_TEAM", "ENG")
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
    })
    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: [
        {"id": "c1", "body": "Use drawer navigation.", "user": {"name": "Alice"}}
    ])
    # answer_decision_request (below, via poll_sources_once) triggers mark_resolved ->
    # move_to_state; mock it too or this makes a real call to the live Linear API.
    monkeypatch.setattr(sources_module.linear_client, "move_to_state", lambda issue_id, type_or_name: None)

    repo_path = tmp_path / "checkout"
    repo_path.mkdir()
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", str(repo_path))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?",
    )

    from apo.web import app as app_module
    await app_module.poll_sources_once()

    decision = db.get_decision_request(conn, decision_id)
    assert decision["status"] == "answered"
    assert decision["answer_text"] == "**Alice:** Use drawer navigation."
    assert decision["answer_source"] == "linear"

    index = (repo_path / ".claude" / "decisions" / "INDEX.md").read_text()
    assert f"| ANSWERED | {decision_id:04d} |" in index
    body = (repo_path / ".claude" / "decisions" / f"{decision_id:04d}-q.md").read_text()
    assert "**Alice:** Use drawer navigation." in body
    assert "https://linear.app/x/ENG-1" in body


async def test_poll_sources_once_retries_a_previously_failed_push(db_path, config_module, sources_module, monkeypatch):
    monkeypatch.setenv("APO_LINEAR_API_KEY", "fake-key")
    monkeypatch.setenv("APO_LINEAR_TEAM", "ENG")
    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: [])

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    conn.execute(
        "UPDATE decision_requests SET linear_push_error = 'timed out', linear_next_retry_at = NULL WHERE id = ?",
        (decision_id,),
    )
    conn.commit()
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: {
        "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/ENG-1",
    })

    from apo.web import app as app_module
    await app_module.poll_sources_once()

    decision = db.get_decision_request(conn, decision_id)
    assert decision["linear_issue_id"] == "iss-1"
    assert decision["linear_push_error"] is None


async def test_poll_sources_once_skips_a_disabled_connector_entirely(db_path, config_module, sources_module, monkeypatch):
    monkeypatch.setenv("APO_LINEAR_API_KEY", "fake-key")
    monkeypatch.setenv("APO_LINEAR_TEAM", "ENG")

    conn = db.get_connection()
    db.set_connector_enabled(conn, "linear", False)
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    conn.execute(
        "UPDATE decision_requests SET linear_push_error = 'timed out' WHERE id = ?", (decision_id,)
    )
    conn.commit()

    calls = []
    monkeypatch.setattr(sources_module.linear_client, "create_issue", lambda project_name, decision: calls.append(1))
    monkeypatch.setattr(sources_module.linear_client, "get_comments", lambda issue_id: calls.append(1))

    from apo.web import app as app_module
    await app_module.poll_sources_once()

    assert calls == []
