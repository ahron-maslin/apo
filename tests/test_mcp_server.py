from apo import mcp_server


def test_create_decision_request_tool(db_path):
    result = mcp_server.create_decision_request(
        project="acme",
        title="Clarify mobile navigation",
        question="Bottom tabs, drawer, or sidebar?",
        context="Desktop implementation is complete.",
        blocked_work=["MobileMenu.tsx"],
        safe_work=["Backend"],
        recommendation="Drawer navigation.",
        confidence=72,
        resume_plan={"objective": "Implement mobile nav", "remaining_steps": ["Implement drawer"]},
    )
    assert "id" in result

    open_questions = mcp_server.list_open_questions(project="acme")
    assert len(open_questions) == 1
    assert open_questions[0]["title"] == "Clarify mobile navigation"
    assert open_questions[0]["resume_plan"]["objective"] == "Implement mobile nav"


def test_create_decision_request_tool_marks_a_registered_session_blocked(db_path, tmp_path):
    from apo import db

    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    result = mcp_server.create_decision_request(
        project="acme", title="Q", question="Q?", session_id="sess-1",
    )

    session = db.list_sessions(conn)[0]
    assert session["status"] == "blocked"
    assert session["current_blocker_id"] == result["id"]


def test_create_request_tools_use_the_live_session_not_the_stale_env_var(db_path, tmp_path, monkeypatch):
    """CLAUDE_CODE_SESSION_ID in a long-lived MCP server's env is whatever session
    spawned it; after /clear the same process serves a different session."""
    from apo import db

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "stale-from-env")
    monkeypatch.setattr(mcp_server.claude_runtime, "current_session_id", lambda pid=None: "live-session")

    decision = mcp_server.create_decision_request(project="acme", title="Q", question="Q?")
    action = mcp_server.create_action_request(project="acme", title="A", question="A?")

    conn = db.get_connection()
    assert db.get_decision_request(conn, decision["id"])["origin_session_id"] == "live-session"
    assert db.get_decision_request(conn, action["id"])["origin_session_id"] == "live-session"


def test_create_request_tools_fall_back_to_the_env_var(db_path, monkeypatch):
    from apo import db

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env")
    monkeypatch.setattr(mcp_server.claude_runtime, "current_session_id", lambda pid=None: None)

    decision = mcp_server.create_decision_request(project="acme", title="Q", question="Q?")
    assert db.get_decision_request(db.get_connection(), decision["id"])["origin_session_id"] == "from-env"


def test_filing_under_a_new_name_from_a_registered_folder_joins_its_project(db_path, tmp_path):
    """The tests' cwd is tmp_path (conftest); an agent there calling its project by a
    name nothing knows yet is the folder's project, not a new one."""
    from apo import db

    conn = db.get_connection()
    owner = db.get_or_create_project(conn, "Umbrella")
    repo = db.create_repository(conn, owner["id"], "Umbrella", str(tmp_path))

    decision = mcp_server.create_decision_request(project="Hooli", title="Q", question="Q?")
    action = mcp_server.create_action_request(project="Hooli", title="A", question="A?")

    for request_id in (decision["id"], action["id"]):
        row = db.get_decision_request(conn, request_id)
        assert row["project_id"] == owner["id"]
        assert row["repository_id"] == repo["id"]
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_every_filing_from_a_registered_folder_gets_its_repository(db_path, tmp_path):
    from apo import db

    first = mcp_server.create_decision_request(project="acme", title="Q1", question="Q?")
    second = mcp_server.create_decision_request(project="acme", title="Q2", question="Q?")

    conn = db.get_connection()
    repo_ids = {db.get_decision_request(conn, i["id"])["repository_id"] for i in (first, second)}
    assert len(repo_ids) == 1 and None not in repo_ids


def test_create_decision_request_tool_takes_a_recommended_option(db_path):
    from apo import db

    decision = mcp_server.create_decision_request(
        project="acme", title="Q", question="Q?",
        options=[{"label": "Postgres", "consequence": "add a compose service", "reversible": True}, "SQLite"],
        recommended_option=0,
    )

    row = db.get_decision_request(db.get_connection(), decision["id"])
    assert row["recommended_option"] == 0
    assert row["options"][0]["consequence"] == "add a compose service"


def test_an_agent_creates_an_outcome_with_mixed_steps(db_path, tmp_path, monkeypatch):
    from apo import db

    monkeypatch.setattr(mcp_server.claude_runtime, "current_session_id", lambda pid=None: "agent-1")
    db.register_session(db.get_connection(), "agent-1", str(tmp_path))

    result = mcp_server.create_outcome(
        project="acme", title="Launch the site", done_when="Live with a real payment",
        steps=[
            {"title": "Configure the build", "owner": "agent"},
            {"title": "Add the DNS record", "owner": "me", "detail": "TXT record for Resend"},
            {"title": "DNS propagation", "owner": "external", "waiting_on": "Cloudflare"},
        ],
    )

    conn = db.get_connection()
    outcome = db.get_outcome(conn, result["id"])
    assert outcome["created_by"] == "agent" and outcome["done_when"] == "Live with a real payment"
    progress = db.outcome_progress(conn, result["id"])
    assert [s["owner"] for s in progress["steps"]] == ["agent", "me", "external"]
    assert result["step_ids"] == [s["id"] for s in progress["steps"]]
    assert db.list_sessions(conn)[0]["outcome_id"] == result["id"]


def test_add_step_tool_with_a_dependency(db_path):
    from apo import db

    outcome = mcp_server.create_outcome(project="acme", title="O")
    first = mcp_server.add_step(outcome_id=outcome["id"], title="First", owner="me")
    second = mcp_server.add_step(outcome_id=outcome["id"], title="Second", owner="me", blocked_by=[first["id"]])

    assert db.open_blockers(db.get_connection(), second["id"]) == [first["id"]]
    assert "error" in mcp_server.add_step(outcome_id=outcome["id"], title="Bad", owner="nobody")


def test_task_tools_list_claim_and_complete(db_path, monkeypatch):
    from apo import db

    monkeypatch.setattr(mcp_server.claude_runtime, "current_session_id", lambda pid=None: "agent-1")
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    task = db.create_task(conn, project_id=project["id"], title="Add rate limiting", detail="100 rpm")

    listed = mcp_server.list_tasks(project="acme")
    assert [t["id"] for t in listed] == [task] and listed[0]["detail"] == "100 rpm"
    assert mcp_server.claim_task(task_id=task) == {"claimed": True}
    assert mcp_server.claim_task(task_id=task) == {"claimed": False}
    assert mcp_server.complete_task(task_id=task, note="done") == {"completed": True}
    assert mcp_server.list_tasks(project="acme") == []


def test_withdraw_and_supersede_tools(db_path):
    from apo import db

    first = mcp_server.create_decision_request(project="acme", title="Round 1", question="Q?")
    second = mcp_server.create_decision_request(project="acme", title="Round 2", question="Q?", supersedes=first["id"])
    conn = db.get_connection()
    assert db.get_decision_request(conn, first["id"])["status"] == "cancelled"

    assert mcp_server.withdraw_request(request_id=second["id"], reason="Figured it out") == {"withdrawn": True}
    assert db.get_decision_request(conn, second["id"])["cancel_reason"] == "Figured it out"


def test_create_action_request_tool(db_path):
    result = mcp_server.create_action_request(
        project="acme", title="Grant S3 access",
        question="Please grant write access to the deploy bucket.",
        blocked_work=["Deploy step"], safe_work=["Write the deploy script"],
    )
    assert "id" in result

    from apo import db
    conn = db.get_connection()
    row = db.get_decision_request(conn, result["id"])
    assert row["kind"] == "action"
    assert row["options"] is None
    assert row["recommendation"] is None
    assert row["confidence"] is None
    assert row["status"] == "open"


def test_create_action_request_tool_marks_a_registered_session_blocked(db_path, tmp_path):
    from apo import db

    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    result = mcp_server.create_action_request(
        project="acme", title="Grant access", question="Please grant access.", session_id="sess-1",
    )

    session = db.list_sessions(conn)[0]
    assert session["status"] == "blocked"
    assert session["current_blocker_id"] == result["id"]


def test_create_decision_request_tool_passes_through_summary(db_path):
    decision_id = mcp_server.create_decision_request(
        project="acme", title="Q", question="Q?",
        summary="Short human-readable summary.",
    )["id"]

    from apo import db
    conn = db.get_connection()
    decision = db.get_decision_request(conn, decision_id)
    assert decision["summary"] == "Short human-readable summary."


def test_list_resolved_questions_excludes_open(db_path):
    open_id = mcp_server.create_decision_request(project="acme", title="Open one", question="Q1")["id"]
    answered_id = mcp_server.create_decision_request(project="acme", title="Answered one", question="Q2")["id"]

    from apo import db
    conn = db.get_connection()
    db.answer_decision_request(conn, answered_id, "An answer")

    resolved = mcp_server.list_resolved_questions(project="acme")
    assert [d["id"] for d in resolved] == [answered_id]

    open_only = mcp_server.list_open_questions(project="acme")
    assert [d["id"] for d in open_only] == [open_id]


def test_create_decision_request_auto_links_claude_code_session(db_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-detected-session")
    decision_id = mcp_server.create_decision_request(project="acme", title="Q", question="Q?")["id"]

    from apo import db
    conn = db.get_connection()
    decision = db.get_decision_request(conn, decision_id)
    assert decision["origin_session_id"] == "env-detected-session"


def test_create_decision_request_auto_registers_repository_when_project_has_none(db_path, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    decision_id = mcp_server.create_decision_request(project="acme", title="Q", question="Q?")["id"]

    from apo import db
    conn = db.get_connection()
    decision = db.get_decision_request(conn, decision_id)
    assert decision["repository_id"] is not None
    repo = db.get_repository(conn, decision["repository_id"])
    assert repo["local_path"] == str(tmp_path)


def test_create_decision_request_does_not_guess_among_existing_repositories(db_path):
    from apo import db
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", "/some/explicit/backend")
    db.create_repository(conn, project["id"], "frontend", "/some/explicit/frontend")

    decision_id = mcp_server.create_decision_request(project="acme", title="Q", question="Q?")["id"]
    decision = db.get_decision_request(conn, decision_id)
    assert decision["repository_id"] is None


def test_create_decision_request_uses_a_projects_only_repository_explicitly(db_path):
    """Not a guess, and recorded on the row rather than left to a read-time fallback
    that silently stops working once the project gains a second repository."""
    from apo import db
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", "/some/explicit/backend")

    decision_id = mcp_server.create_decision_request(project="acme", title="Q", question="Q?")["id"]
    assert db.get_decision_request(conn, decision_id)["repository_id"] == repo["id"]


def test_create_decision_request_explicit_session_id_wins(db_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-detected-session")
    decision_id = mcp_server.create_decision_request(
        project="acme", title="Q", question="Q?", session_id="explicit-session",
    )["id"]

    from apo import db
    conn = db.get_connection()
    decision = db.get_decision_request(conn, decision_id)
    assert decision["origin_session_id"] == "explicit-session"


def test_answer_decision_request_tool_records_and_writes_to_repo(db_path, tmp_path):
    """Answering conversationally in chat has to land in the repo exactly like a
    dashboard or Linear answer — this is the path that exists so a decision settled in
    conversation doesn't leave the DB open and the repo none the wiser."""
    from apo import db

    repo_path = tmp_path / "checkout"
    repo_path.mkdir()
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", str(repo_path))
    decision_id = mcp_server.create_decision_request(
        project="acme", title="Clarify mobile navigation", question="Bottom tabs or drawer?",
        repository="backend",
    )["id"]
    assert db.get_decision_request(conn, decision_id)["repository_id"] == repo["id"]

    result = mcp_server.answer_decision_request(decision_id, "Use drawer navigation.")
    assert result["recorded"] is True

    decision = db.get_decision_request(conn, decision_id)
    assert decision["status"] == "answered"
    assert decision["answer_source"] == "mcp"

    index = (repo_path / ".claude" / "decisions" / "INDEX.md").read_text()
    assert f"| ANSWERED | {decision_id:04d} |" in index


def test_answer_decision_request_tool_is_idempotent(db_path):
    decision_id = mcp_server.create_decision_request(project="acme", title="Q", question="Q?")["id"]

    first = mcp_server.answer_decision_request(decision_id, "First answer")
    second = mcp_server.answer_decision_request(decision_id, "Second answer")

    assert first["recorded"] is True
    assert second["recorded"] is False


def test_mark_decision_resolved_requires_answered_state(db_path):
    from apo import db

    decision_id = mcp_server.create_decision_request(project="acme", title="Q", question="Q?")["id"]

    too_early = mcp_server.mark_decision_resolved(decision_id)
    assert too_early["resolved"] is False

    conn = db.get_connection()
    db.answer_decision_request(conn, decision_id, "An answer")

    resolved = mcp_server.mark_decision_resolved(decision_id)
    assert resolved["resolved"] is True
    assert db.get_decision_request(conn, decision_id)["status"] == "resolved"
