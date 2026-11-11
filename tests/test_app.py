import re
from fastapi.testclient import TestClient

from apo.web import app as app_module


def make_client(db_path):
    return TestClient(app_module.app)


def test_create_list_answer_roundtrip(db_path):
    client = make_client(db_path)

    create_resp = client.post(
        "/api/decisions",
        json={
            "project": "acme",
            "title": "Clarify mobile navigation",
            "question": "Bottom tabs, drawer, or sidebar?",
            "blocked_work": ["MobileMenu.tsx"],
            "safe_work": ["Backend"],
            "recommendation": "Drawer navigation.",
            "confidence": 72,
        },
    )
    assert create_resp.status_code == 200
    decision_id = create_resp.json()["id"]

    open_list = client.get("/api/decisions", params={"status": "open"})
    assert [d["id"] for d in open_list.json()] == [decision_id]

    dashboard_resp = client.get("/decisions")
    assert dashboard_resp.status_code == 200
    assert "Clarify mobile navigation" in dashboard_resp.text

    detail_resp = client.get(f"/decisions/{decision_id}")
    assert detail_resp.status_code == 200
    assert "Drawer navigation." in detail_resp.text

    answer_resp = client.post(
        f"/decisions/{decision_id}/answer",
        data={"answer_text": "Use drawer navigation."},
    )
    assert answer_resp.status_code == 200  # after following the redirect

    answered = client.get(f"/api/decisions/{decision_id}").json()
    assert answered["status"] == "answered"
    assert answered["answer_text"] == "Use drawer navigation."

    open_list_after = client.get("/api/decisions", params={"status": "open"})
    assert open_list_after.json() == []


def test_summary_shown_on_dashboard_and_detail_with_full_context_folded_away(db_path):
    """The dashboard/detail pages are for a human deciding an answer, not the agent
    that resumes the work — they should lead with the short summary, and the
    resume-plan handoff detail (which is written in full into the repo's markdown
    for the resuming agent) should not be dumped in the open by default."""
    client = make_client(db_path)

    create_resp = client.post(
        "/api/decisions",
        json={
            "project": "acme",
            "title": "Clarify mobile navigation",
            "question": "Bottom tabs, drawer, or sidebar?",
            "summary": "Pick a mobile nav pattern to unblock MobileMenu.tsx.",
            "resume_plan": {
                "objective": "Implement mobile navigation",
                "remaining_steps": ["Implement drawer", "Update layout"],
            },
        },
    )
    decision_id = create_resp.json()["id"]

    dashboard_resp = client.get("/decisions")
    assert "Pick a mobile nav pattern to unblock MobileMenu.tsx." in dashboard_resp.text

    detail_resp = client.get(f"/decisions/{decision_id}")
    assert "Pick a mobile nav pattern to unblock MobileMenu.tsx." in detail_resp.text
    assert "<details" in detail_resp.text


def test_decision_not_found_returns_404(db_path):
    client = make_client(db_path)
    resp = client.get("/decisions/999")
    assert resp.status_code == 404


def test_archived_project_hidden_from_default_view_but_reachable_directly(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    active = db.get_or_create_project(conn, "active-project")
    db.create_decision_request(conn, project_id=active["id"], title="Active Q", question="Q?")
    archived = db.get_or_create_project(conn, "old-project")
    db.create_decision_request(conn, project_id=archived["id"], title="Old Q", question="Q?")
    db.archive_project(conn, archived["id"])

    default_resp = client.get("/projects")
    assert "active-project" in default_resp.text and "old-project" not in default_resp.text
    with_archived = client.get("/projects", params={"archived": 1}).text
    assert with_archived.index("active-project") < with_archived.index("Archived") < with_archived.index("old-project")

    direct_resp = client.get("/decisions", params={"project": "old-project"})
    assert "Old Q" in direct_resp.text


def test_answering_via_dashboard_writes_into_the_repo(db_path, tmp_path):
    """The dashboard answer route is fire-and-forget now: it records the answer, the
    markdown lands, and the request is done — nothing is spawned and there is no job
    to watch, which is the whole point of this design."""
    from apo import db

    repo_path = tmp_path / "checkout"
    repo_path.mkdir()
    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", str(repo_path))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"],
        title="Clarify mobile navigation", question="Bottom tabs or drawer?",
    )

    resp = client.post(f"/decisions/{decision_id}/answer", data={"answer_text": "Use drawer navigation."})
    assert resp.status_code == 200

    assert db.get_decision_request(conn, decision_id)["status"] == "answered"
    index = (repo_path / ".claude" / "decisions" / "INDEX.md").read_text()
    assert f"| ANSWERED | {decision_id:04d} |" in index

    # The detail page tells you where it went instead of showing a job log to babysit.
    detail = client.get(f"/decisions/{decision_id}")
    assert str(repo_path) in detail.text
    assert "Nothing is running in that repo to pick it up yet" in detail.text


def test_now_reports_only_fresh_sync_errors(db_path):
    from datetime import datetime, timedelta, timezone

    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    conn.execute(
        "INSERT INTO sync_errors (occurred_at, source, decision_request_id, message) VALUES (?, ?, ?, ?)",
        (old_time, "linear_push", None, "a stale push failure"),
    )
    conn.commit()
    db.record_sync_error(conn, "linear_push", None, "a fresh push failure")

    resp = client.get("/")

    assert "a fresh push failure" in resp.text
    assert "a stale push failure" not in resp.text


def test_decisions_page_sync_banner_hides_resolved_errors(db_path):
    from datetime import datetime, timedelta, timezone

    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    db.record_sync_error(conn, "linear_poll", None, "a timeout that has since recovered")
    db.resolve_sync_errors(
        conn, ["linear_poll"], before=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    )

    resp = client.get("/decisions", params={"project": "acme"})

    assert "a timeout that has since recovered" not in resp.text
    assert "recent sync error" not in resp.text


def test_settings_page_lists_connectors_with_configured_and_enabled_state(db_path, monkeypatch):
    # app_module (module-level import at the top of this file) is bound once at
    # collection time, before db_path's env-blanking runs, so it can see this
    # checkout's real .env — force a known state instead of relying on ambient
    # environment. Deliberately NOT re-importing app here: db_path already popped
    # "app" from sys.modules, so a fresh `import app` would bind a different module
    # object than the one make_client()'s closure actually uses.
    monkeypatch.setattr(app_module.sources.LinearSource, "is_configured", lambda self: False)

    client = make_client(db_path)
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "linear" in resp.text.lower()
    assert "not configured" in resp.text.lower()


def test_connect_integration_success_stores_credentials_and_redirects(db_path, monkeypatch):
    from apo import credentials
    from apo import db

    monkeypatch.setattr(
        app_module.linear_client, "validate_credentials",
        lambda api_key, team: {"display_name": "Test User → Engineering", "team_id": "team-1"},
    )

    client = make_client(db_path)
    resp = client.post(
        "/settings/integrations/linear/connect",
        data={"api_key": "typed-key", "team": "ENG"},
    )

    assert resp.status_code == 200  # after following the redirect
    conn = db.get_connection()
    assert credentials.get_credentials(conn, "linear") == {"api_key": "typed-key", "team": "ENG"}
    assert credentials.get_display_name(conn, "linear") == "Test User → Engineering"


def test_connect_integration_failure_rerenders_with_error_and_does_not_store(db_path, monkeypatch):
    from apo import credentials
    from apo import db

    def boom(api_key, team):
        raise RuntimeError("Linear API error 401: Unauthorized")

    monkeypatch.setattr(app_module.linear_client, "validate_credentials", boom)

    client = make_client(db_path)
    resp = client.post(
        "/settings/integrations/linear/connect",
        data={"api_key": "bad-key", "team": "ENG"},
    )

    assert resp.status_code == 400
    assert "could not verify" in resp.text.lower()
    # The generic reason is shown, not the raw exception text (which could in
    # principle echo back request/response details tied to the submitted key).
    assert "401" not in resp.text
    assert "Unauthorized" not in resp.text
    # Re-rendering (not redirecting) means the submitted values survive the
    # failed attempt — the user fixes the one field that was wrong rather than
    # retyping the key from scratch.
    assert 'value="bad-key"' in resp.text

    conn = db.get_connection()
    assert credentials.get_credentials(conn, "linear") is None


def test_disconnect_integration_removes_stored_credentials(db_path):
    from apo import credentials
    from apo import db

    conn = db.get_connection()
    credentials.set_credentials(conn, "linear", {"api_key": "k", "team": "ENG"}, display_name="x")

    client = make_client(db_path)
    resp = client.post("/settings/integrations/linear/disconnect")

    assert resp.status_code == 200  # after following the redirect
    assert credentials.get_credentials(conn, "linear") is None


def test_connect_integration_clears_stale_linear_id_caches(db_path, monkeypatch):
    """A team/project/state id resolved under the previous credential must not
    silently keep being used after reconnecting with a different one — see
    linear_client.clear_caches()."""
    monkeypatch.setattr(app_module.linear_client, "_team_id_cache", "stale-team-id")
    app_module.linear_client._project_id_cache["Some Project"] = "stale-project-id"
    app_module.linear_client._state_id_cache[("stale-team-id", "completed")] = "stale-state-id"
    monkeypatch.setattr(
        app_module.linear_client, "validate_credentials",
        lambda api_key, team: {"display_name": "Test User → Engineering", "team_id": "team-1"},
    )

    client = make_client(db_path)
    client.post("/settings/integrations/linear/connect", data={"api_key": "typed-key", "team": "ENG"})

    assert app_module.linear_client._team_id_cache is None
    assert app_module.linear_client._project_id_cache == {}
    assert app_module.linear_client._state_id_cache == {}


def test_disconnect_integration_clears_stale_linear_id_caches(db_path, monkeypatch):
    monkeypatch.setattr(app_module.linear_client, "_team_id_cache", "stale-team-id")

    client = make_client(db_path)
    client.post("/settings/integrations/linear/disconnect")

    assert app_module.linear_client._team_id_cache is None


def test_test_integration_success_refreshes_display_name(db_path, monkeypatch):
    from apo import credentials
    from apo import db

    conn = db.get_connection()
    credentials.set_credentials(conn, "linear", {"api_key": "k", "team": "ENG"}, display_name="Old Name")
    monkeypatch.setattr(
        app_module.linear_client, "validate_credentials",
        lambda api_key, team: {"display_name": "New Name", "team_id": "team-1"},
    )

    client = make_client(db_path)
    resp = client.post("/settings/integrations/linear/test")

    assert resp.status_code == 200  # after following the redirect
    assert credentials.get_display_name(conn, "linear") == "New Name"


def test_test_integration_failure_shows_error_without_clearing_credentials(db_path, monkeypatch):
    from apo import credentials
    from apo import db

    conn = db.get_connection()
    credentials.set_credentials(conn, "linear", {"api_key": "k", "team": "ENG"}, display_name="Still Good")

    def boom(api_key, team):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(app_module.linear_client, "validate_credentials", boom)

    client = make_client(db_path)
    resp = client.post("/settings/integrations/linear/test")

    assert resp.status_code == 400
    assert "could not verify" in resp.text.lower()
    assert credentials.get_credentials(conn, "linear") == {"api_key": "k", "team": "ENG"}
    assert credentials.get_display_name(conn, "linear") == "Still Good"


def test_test_integration_without_stored_credentials_shows_error(db_path):
    client = make_client(db_path)
    resp = client.post("/settings/integrations/linear/test")

    assert resp.status_code == 400
    assert "not connected" in resp.text.lower()


def test_unknown_provider_rejected_with_404_on_every_integration_route(db_path):
    client = make_client(db_path)

    assert client.post("/settings/integrations/jira/connect", data={}).status_code == 404
    assert client.post("/settings/integrations/jira/disconnect").status_code == 404
    assert client.post("/settings/integrations/jira/test").status_code == 404


def test_settings_page_shows_connected_as_display_name_when_db_credential_present(db_path):
    from apo import credentials
    from apo import db

    conn = db.get_connection()
    credentials.set_credentials(conn, "linear", {"api_key": "k", "team": "ENG"}, display_name="Test User → Engineering")

    client = make_client(db_path)
    resp = client.get("/settings")

    assert resp.status_code == 200
    assert "Test User → Engineering" in resp.text


def test_toggle_connector_flips_current_state(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    assert db.is_connector_enabled(conn, "linear") is True

    client.post("/settings/connectors/linear/toggle")
    assert db.is_connector_enabled(conn, "linear") is False

    client.post("/settings/connectors/linear/toggle")
    assert db.is_connector_enabled(conn, "linear") is True


def test_fleet_view_at_root_shows_sessions_grouped_by_project(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))

    resp = client.get("/sessions")

    assert resp.status_code == 200
    assert "acme" in resp.text
    assert "sess-1" in resp.text or "Running" in resp.text


def test_fleet_view_shows_unregistered_sessions_bucket(db_path):
    """A session with no cwd at all (register_session can't auto-register a
    repo for a path it doesn't have) is the one case that still lands in the
    Unregistered bucket — a session whose cwd just doesn't match anything yet
    gets auto-registered instead, see test_sessions.py."""
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "sess-ghost", None)

    resp = client.get("/sessions")

    assert resp.status_code == 200
    assert "Unregistered" in resp.text


def test_fleet_view_links_blocked_session_to_its_blocker(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Pick a provider", question="Q?", origin_session_id="sess-1",
    )

    resp = client.get("/sessions")

    assert f"/decisions/{decision_id}" in resp.text
    assert "Pick a provider" in resp.text


def test_fleet_view_says_how_many_more_requests_a_session_is_waiting_on(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    first = db.create_decision_request(
        conn, project_id=project["id"], title="Oldest ask", question="Q?", origin_session_id="sess-1",
    )
    for title in ("Second ask", "Third ask"):
        db.create_decision_request(
            conn, project_id=project["id"], title=title, question="Q?", origin_session_id="sess-1",
        )

    resp = client.get("/sessions")

    assert f"/decisions/{first}" in resp.text
    assert "Oldest ask" in resp.text
    assert "+2 more" in resp.text


def test_home_is_now_and_shows_what_needs_you_across_projects(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    acme = db.get_or_create_project(conn, "acme")
    db.create_decision_request(
        conn, project_id=acme["id"], title="Pick a queue", question="Postgres or SQLite?",
        options=["Postgres", "SQLite"], recommendation="Postgres.", confidence=85,
    )
    db.register_session(conn, "waiting", str(tmp_path / "globex"))
    db.session_waiting(conn, "waiting", "permission", "Bash: stripe listen")

    text = client.get("/").text

    assert "<title>(2) Now — apo</title>" in text
    assert "Your move" in text
    assert "Pick a queue" in text and "acme" in text
    assert "stripe listen" in text and "globex" in text
    assert 'name="next" value="/"' in text  # answering from Now comes back to Now
    assert 'value="recommendation"' in text


def test_now_says_when_nothing_needs_you(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "worker", str(tmp_path / "acme"))
    db.session_heartbeat(conn, "worker")

    text = client.get("/").text

    assert "Nothing needs you" in text
    assert "1 working" in text


def test_marking_a_finished_session_reviewed_clears_it(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    session_id = db.register_session(conn, "done", str(tmp_path / "acme"))
    db.session_heartbeat(conn, "done")
    db.session_stop(conn, "done")
    assert "Finished — not looked at" in client.get("/").text

    resp = client.post(f"/sessions/{session_id}/reviewed", follow_redirects=False)

    assert resp.status_code == 303 and resp.headers["location"] == "/"
    assert db.now_queue(db.get_connection())["review"] == []


def test_api_now_returns_the_ranked_lanes(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    acme = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=acme["id"], title="Q", question="Q?")

    data = client.get("/api/now").json()

    assert [i["key"] for i in data["your_move"]] == [f"r:{decision_id}"]
    assert data["count"] == 1
    assert set(data) >= {"problems", "your_move", "asks", "in_transit", "review", "quiet"}


def test_decisions_without_a_project_lists_every_projects_open_ones(db_path):
    """It used to open on the alphabetically first project and could say "You're all
    caught up" while other projects had open decisions."""
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    for name, title in (("alpha", "Nothing here"), ("zeta", "Pick a queue"), ("mid", "Rotate key")):
        project = db.get_or_create_project(conn, name)
        decision_id = db.create_decision_request(conn, project_id=project["id"], title=title, question="Q?")
        if name == "alpha":
            db.answer_decision_request(conn, decision_id, "done")

    text = client.get("/decisions").text
    waiting = text.split("Decision log")[0]

    assert "All projects" in text
    assert "Pick a queue" in waiting and "Rotate key" in waiting
    assert "Nothing here" not in waiting
    assert "all caught up" not in text.lower()


def test_palette_data_covers_pages_projects_open_requests_and_sessions(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    acme = db.get_or_create_project(conn, "acme")
    open_id = db.create_decision_request(
        conn, project_id=acme["id"], title="Pick a queue", question="Q?", options=["Postgres", "SQLite"],
        recommendation="Postgres.",
    )
    answered = db.create_decision_request(conn, project_id=acme["id"], title="Old", question="Q?")
    db.answer_decision_request(conn, answered, "x")
    db.register_session(conn, "sess-1", str(tmp_path / "acme"))

    data = client.get("/api/palette").json()

    assert {"Now", "Projects", "Agents", "Decisions", "Activity", "Settings"} <= {p["label"] for p in data["pages"]}
    assert "acme" in {p["name"] for p in data["projects"]}
    assert [r["id"] for r in data["requests"]] == [open_id]
    assert data["requests"][0]["options"] == ["Postgres", "SQLite"]
    assert data["requests"][0]["recommendation"] == "Postgres."
    assert data["sessions"][0]["href"].startswith("/projects/agents#session-card-")


def test_every_page_carries_the_command_palette(db_path):
    client = make_client(db_path)
    for path in ("/", "/sessions", "/decisions", "/activity", "/settings"):
        text = client.get(path).text
        assert "data-palette" in text and "/static/palette.js" in text, path
        assert 'id="shortcuts"' in text, path


def test_sessions_page_is_the_fleet(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    db.register_session(db.get_connection(), "sess-1", str(tmp_path))
    resp = client.get("/sessions")
    assert resp.url.path == "/projects/agents" and 'id="session-card-' in resp.text


def _open_decision(**fields):
    from apo import db

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    return db.create_decision_request(
        conn, project_id=project["id"], **{"title": "Queue backend", "question": "Postgres or SQLite?", **fields},
    )


def test_picking_an_option_answers_with_one_click(db_path):
    from apo import db

    client = make_client(db_path)
    decision_id = _open_decision(options=["Postgres", "SQLite"])

    resp = client.post(f"/decisions/{decision_id}/answer", data={"option": "1", "answer_text": ""}, follow_redirects=False)

    assert resp.status_code == 303
    row = db.get_decision_request(db.get_connection(), decision_id)
    assert row["answer_text"] == "Chose option 2: SQLite"
    assert row["answer_kind"] == "option"


def test_accepting_the_recommendation_answers_with_one_click(db_path):
    from apo import db

    client = make_client(db_path)
    decision_id = _open_decision(recommendation="Postgres.")

    client.post(f"/decisions/{decision_id}/answer", data={"choice": "recommendation", "answer_text": "ship it"})

    row = db.get_decision_request(db.get_connection(), decision_id)
    assert row["answer_text"] == "Accepted your recommendation: Postgres.\n\nNote: ship it"


def test_need_more_info_requires_saying_what(db_path):
    client = make_client(db_path)
    decision_id = _open_decision()

    resp = client.post(f"/decisions/{decision_id}/answer", data={"choice": "clarify", "answer_text": ""})
    assert "page-alert" in resp.text
    assert "what you need to know" in resp.text


def test_an_action_can_be_confirmed_done_without_typing(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    action_id = db.create_action_request(conn, project_id=project["id"], title="Add DNS record", question="Q?")

    client.post(f"/decisions/{action_id}/answer", data={"choice": "done", "answer_text": ""})

    assert db.get_decision_request(conn, action_id)["answer_text"] == "Done."


def test_a_typed_answer_still_needs_text(db_path):
    client = make_client(db_path)
    decision_id = _open_decision()
    resp = client.post(f"/decisions/{decision_id}/answer", data={"answer_text": "  "})
    assert "page-alert" in resp.text and "Write an answer" in resp.text


def test_decision_pages_offer_quick_answers(db_path):
    client = make_client(db_path)
    decision_id = _open_decision(options=["Postgres", "Drawer (RECOMMENDED)"], recommendation="Drawer.")

    for page in (f"/decisions/{decision_id}", "/"):
        text = client.get(page).text
        assert 'name="option" value="0"' in text and 'name="option" value="1"' in text, page
        assert 'value="recommendation"' in text and 'value="delegate"' in text, page
        assert 'value="clarify"' in text and 'value="decline"' in text, page
        assert "formnovalidate" in text, page


def test_action_pages_offer_done_without_requiring_text(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    action_id = db.create_action_request(conn, project_id=project["id"], title="Add DNS record", question="Q?")

    text = client.get(f"/decisions/{action_id}").text

    assert 'value="done"' in text
    assert "required" not in text.split('name="answer_text"')[1].split(">")[0]


def test_activity_page_lists_what_happened_newest_first(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    acme = db.get_or_create_project(conn, "acme")
    globex = db.get_or_create_project(conn, "globex")
    first = db.create_decision_request(conn, project_id=acme["id"], title="Pick a queue", question="Q?")
    db.create_decision_request(conn, project_id=globex["id"], title="Rotate the key", question="Q?")
    db.answer_decision_request(conn, first, "Postgres")

    text = client.get("/activity").text

    assert text.index("You answered #1") < text.index("Decision #2 filed: Rotate the key") < text.index("Decision #1 filed")
    assert 'href="/decisions/1"' in text
    filtered = client.get("/activity?project=globex").text
    assert "Rotate the key" in filtered and "Pick a queue" not in filtered


def test_activity_page_hides_routine_turn_ends_unless_asked(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_stop(conn, "sess-1")

    assert "Turn finished" not in client.get("/activity").text
    assert "Turn finished" in client.get("/activity?all=1").text


def test_pages_are_branded_apo(db_path):
    client = make_client(db_path)
    for path in ("/", "/decisions", "/settings", "/guide"):
        text = client.get(path).text
        assert "<title>" in text and " apo</title>" in text, path
        assert "Switchboard" not in text, path
        assert "Control Center" not in text, path
        assert "Decision Orchestrator" not in text, path


def test_answer_button_does_not_promise_to_resume_work(db_path):
    """Answering records the answer and writes it into the repo; nothing resumes
    until a session picks it up."""
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    for page in ("/", "/decisions/1"):
        text = client.get(page).text
        assert "resume work" not in text
        assert "Send answer" in text


def test_fleet_view_shows_what_a_waiting_session_is_waiting_for(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "perm", str(tmp_path))
    db.session_waiting(conn, "perm", "permission", "Bash: stripe listen --forward-to localhost:8000")
    db.register_session(conn, "err", str(tmp_path))
    db.session_stop_failure(conn, "err", "overloaded")

    resp = client.get("/sessions")

    assert "Needs permission" in resp.text
    assert "stripe listen --forward-to localhost:8000" in resp.text
    assert "API error" in resp.text and "overloaded" in resp.text


def test_fleet_view_counts_every_kind_of_waiting_as_needs_you(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "perm", str(tmp_path))
    db.session_waiting(conn, "perm", "permission", "Bash: make")
    db.register_session(conn, "input", str(tmp_path))
    db.session_waiting(conn, "input", "input", "Which one?")
    db.register_session(conn, "resting", str(tmp_path))
    db.session_stop(conn, "resting")

    resp = client.get("/sessions")

    assert 'data-stat="needs_you">2<' in resp.text


def test_fleet_view_never_calls_an_idle_session_stalled(db_path, tmp_path):
    from datetime import datetime, timedelta, timezone

    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "resting", str(tmp_path))
    db.session_stop(conn, "resting")
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    conn.execute("UPDATE sessions SET last_seen_at = ?", (old,))
    conn.commit()

    resp = client.get("/sessions")

    assert "stalled" not in resp.text.lower()
    assert "crashed" not in resp.text.lower()


def test_fleet_view_hides_ghost_sessions(db_path, tmp_path):
    from datetime import datetime, timedelta, timezone

    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "ghost-session", str(tmp_path / "ghostproj"))
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn.execute("UPDATE sessions SET started_at = ?, last_seen_at = ?", (old, old))
    conn.commit()

    resp = client.get("/sessions")

    assert "ghost-session" not in resp.text


def test_viewing_the_fleet_marks_the_dashboard_as_watched(db_path, monkeypatch):
    """The live-session reconciler only runs while someone is looking."""
    client = make_client(db_path)
    monkeypatch.setattr(app_module, "_last_viewed_at", 0.0)

    client.get("/api/sessions")

    assert app_module._last_viewed_at > 0


def test_reconcile_once_feeds_claude_agents_into_the_db(db_path, tmp_path, monkeypatch):
    from apo import db

    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    monkeypatch.setattr(
        app_module.claude_runtime, "list_agents",
        lambda **k: [{"pid": 5, "sessionId": "sess-1", "status": "waiting", "waitingFor": "permission prompt"}],
    )

    app_module.reconcile_once()

    s = db.list_sessions(db.get_connection())[0]
    assert s["display_status"] == "waiting_permission"


def test_api_sessions_returns_display_status_and_human_last_seen(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_heartbeat(conn, "sess-1")

    resp = client.get("/api/sessions")

    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["claude_session_id"] == "sess-1"
    assert data[0]["display_status"] == "running"
    assert data[0]["needs_you"] is False
    assert "last_seen_human" in data[0]
    assert "status_label" in data[0]


def test_fleet_view_shows_title_summary_and_prompt_but_not_path(db_path, tmp_path):
    """Claude Code's auto-title names the card, so sessions in one project can be told
    apart at a glance; the recap jogs your memory. The cwd/repo-path line is only a
    fallback for a session with no recap yet."""
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.record_session_summary(
        conn, "sess-1", "Fixed the token refresh <b>bug</b>; tests pass.",
        title="Login bug fix", last_prompt="why does login fail?",
    )

    resp = client.get("/sessions")

    assert "Fixed the token refresh &lt;b&gt;bug&lt;/b&gt;; tests pass." in resp.text
    assert "why does login fail?" in resp.text
    assert '<h3 class="session-title"' in resp.text and "Login bug fix" in resp.text
    assert '<div class="session-path"' not in resp.text


def test_fleet_card_has_no_empty_title_when_claude_has_not_named_the_session(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.session_heartbeat(conn, "sess-1")

    resp = client.get("/sessions")

    assert 'class="session-title" hidden' in resp.text


def test_fleet_view_shows_path_as_fallback_when_session_has_no_recap_yet(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))

    resp = client.get("/sessions")

    assert '<div class="session-path"' in resp.text


def test_fleet_view_shows_recently_ended_sessions_but_not_old_ones(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    for sid, summary in (("sess-recent", "Shipped the export feature."), ("sess-old", "Ancient work.")):
        db.register_session(conn, sid, str(tmp_path / sid))
        db.record_session_summary(conn, sid, summary)
        db.session_end(conn, sid, "logout")
    conn.execute(
        "UPDATE sessions SET ended_at = '2020-01-01T00:00:00+00:00', last_seen_at = '2020-01-01T00:00:00+00:00' "
        "WHERE claude_session_id = 'sess-old'"
    )
    conn.commit()

    resp = client.get("/sessions")

    assert "Recently ended" in resp.text
    assert "Shipped the export feature." in resp.text
    assert "Ancient work." not in resp.text


def test_api_sessions_includes_last_done_summary(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    db.record_session_summary(conn, "sess-1", "Did a thing.", title="T", last_prompt="P")

    data = client.get("/api/sessions").json()

    assert data[0]["last_summary"] == "Did a thing."
    assert data[0]["title"] == "T"
    assert data[0]["last_prompt"] == "P"
    assert data[0]["last_summary_human"] == "just now"


def test_add_checklist_item_via_dashboard(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    session_id = db.list_sessions(conn)[0]["id"]

    resp = client.post(f"/sessions/{session_id}/checklist", data={"title": "Write tests"})

    assert resp.status_code == 200  # after following the redirect
    items = db.list_checklist_items(conn, session_id)
    assert items[0]["title"] == "Write tests"
    assert items[0]["status"] == "pending"


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def test_upload_attachment_returns_saved_shape_and_serves_file(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    data = PNG_MAGIC + b"fake-png-bytes"

    resp = client.post(
        f"/decisions/{decision_id}/attachments",
        files={"files": ("screenshot.png", data, "image/png")},
    )

    assert resp.status_code == 200
    [attachment] = resp.json()
    assert set(attachment.keys()) == {"id", "filename", "url", "content_type", "size_bytes"}
    assert attachment["filename"] == "screenshot.png"
    assert attachment["content_type"] == "image/png"
    assert attachment["size_bytes"] == len(data)
    assert attachment["url"] == f"/attachments/{attachment['id']}"

    get_resp = client.get(attachment["url"])
    assert get_resp.status_code == 200
    assert get_resp.content == data
    assert get_resp.headers["content-type"] == "image/png"
    assert get_resp.headers["x-content-type-options"] == "nosniff"


def test_upload_attachment_accepts_multiple_files_in_one_request(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    resp = client.post(
        f"/decisions/{decision_id}/attachments",
        files=[
            ("files", ("one.png", PNG_MAGIC + b"one", "image/png")),
            ("files", ("two.jpg", JPEG_MAGIC + b"two", "image/jpeg")),
        ],
    )

    assert resp.status_code == 200
    assert [a["filename"] for a in resp.json()] == ["one.png", "two.jpg"]


def test_upload_attachment_rejects_whole_batch_if_any_file_is_invalid(db_path):
    """Multi-file atomicity: a bad file anywhere in the batch must not leave any
    earlier file in the same request persisted."""
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    resp = client.post(
        f"/decisions/{decision_id}/attachments",
        files=[
            ("files", ("good.png", PNG_MAGIC + b"one", "image/png")),
            ("files", ("bad.png", b"not-actually-a-png", "image/png")),
        ],
    )

    assert resp.status_code == 400
    assert db.list_attachments(conn, decision_id) == []


def test_upload_attachment_rejects_content_type_mismatching_actual_bytes(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    resp = client.post(
        f"/decisions/{decision_id}/attachments",
        files={"files": ("screenshot.png", b"<script>alert(1)</script>", "image/png")},
    )

    assert resp.status_code == 400


def test_upload_attachment_404s_for_unknown_decision(db_path):
    client = make_client(db_path)
    resp = client.post(
        "/decisions/999/attachments",
        files={"files": ("screenshot.png", PNG_MAGIC + b"data", "image/png")},
    )
    assert resp.status_code == 404


def test_upload_attachment_rejects_bad_content_type_with_400_not_500(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    resp = client.post(
        f"/decisions/{decision_id}/attachments",
        files={"files": ("evil.svg", b"<svg></svg>", "image/svg+xml")},
    )

    assert resp.status_code == 400


def test_get_attachment_404s_for_unknown_id(db_path):
    client = make_client(db_path)
    resp = client.get("/attachments/999")
    assert resp.status_code == 404


def test_delete_attachment_removes_it_and_404s_on_wrong_decision(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    other_decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q2", question="Q2?")

    upload_resp = client.post(
        f"/decisions/{decision_id}/attachments",
        files={"files": ("screenshot.png", PNG_MAGIC + b"data", "image/png")},
    )
    attachment_id = upload_resp.json()[0]["id"]

    wrong_decision_resp = client.delete(f"/decisions/{other_decision_id}/attachments/{attachment_id}")
    assert wrong_decision_resp.status_code == 404

    delete_resp = client.delete(f"/decisions/{decision_id}/attachments/{attachment_id}")
    assert delete_resp.status_code in (200, 204)

    assert db.list_attachments(conn, decision_id) == []
    assert client.get(f"/attachments/{attachment_id}").status_code == 404


def test_upload_and_delete_attachment_are_locked_once_decision_is_answered(db_path):
    """Attachments are only meant to change while composing an answer — once the
    decision's markdown (and any already-copied attachment files) have been
    written into the target repo, nothing re-syncs that projection on a later
    attachment edit, so add/remove must be rejected rather than silently
    desyncing the dashboard from the repo."""
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    saved = db.save_attachment(conn, decision_id, "screenshot.png", "image/png", PNG_MAGIC + b"data")
    db.answer_decision_request(conn, decision_id, "Here's the answer.")

    upload_resp = client.post(
        f"/decisions/{decision_id}/attachments",
        files={"files": ("another.png", PNG_MAGIC + b"more", "image/png")},
    )
    assert upload_resp.status_code == 409

    delete_resp = client.delete(f"/decisions/{decision_id}/attachments/{saved['id']}")
    assert delete_resp.status_code == 409
    assert db.list_attachments(conn, decision_id) == [saved]


def test_decision_detail_hides_remove_button_once_answered(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    db.save_attachment(conn, decision_id, "screenshot.png", "image/png", PNG_MAGIC + b"data")
    db.answer_decision_request(conn, decision_id, "Here's the answer.")

    resp = client.get(f"/decisions/{decision_id}")

    assert resp.status_code == 200
    assert "/attachments/" in resp.text  # still shown
    assert "data-attachment-remove" not in resp.text  # but not removable


def test_decision_detail_includes_attachments_in_context(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    db.save_attachment(conn, decision_id, "screenshot.png", "image/png", PNG_MAGIC + b"data")

    resp = client.get(f"/decisions/{decision_id}")

    assert resp.status_code == 200
    assert f"/attachments/" in resp.text


def test_complete_checklist_item_via_dashboard(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(tmp_path))
    session_id = db.list_sessions(conn)[0]["id"]
    item_id = db.add_checklist_item(conn, session_id, "Write tests")

    resp = client.post(f"/sessions/{session_id}/checklist/{item_id}/complete")

    assert resp.status_code == 200
    item = db.list_checklist_items(conn, session_id)[0]
    assert item["status"] == "done"
    assert item["completed_by"] == "human"


def test_nav_shows_open_decision_count_excluding_archived_projects(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    active = db.get_or_create_project(conn, "acme")
    db.create_decision_request(conn, project_id=active["id"], title="One", question="Q?")
    db.create_decision_request(conn, project_id=active["id"], title="Two", question="Q?")
    answered = db.create_decision_request(conn, project_id=active["id"], title="Three", question="Q?")
    db.answer_decision_request(conn, answered, "Done.")
    archived = db.get_or_create_project(conn, "old")
    db.create_decision_request(conn, project_id=archived["id"], title="Old", question="Q?")
    db.archive_project(conn, archived["id"])

    resp = client.get("/projects")  # the count sits on the Decisions tab

    tabs = re.search(r'<nav class="project-tabs".*?</nav>', resp.text, re.S).group(0)
    assert re.search(r'Decisions <span class="count-chip">2</span>', tabs)


def test_decision_detail_links_back_to_its_project(db_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme web")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    resp = client.get(f"/decisions/{decision_id}")

    assert f"/projects/{project['id']}/decisions" in resp.text


def test_fleet_project_section_links_to_what_waits_on_you(db_path, tmp_path):
    from apo import db

    client = make_client(db_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_repository(conn, project["id"], "backend", str(tmp_path))
    db.register_session(conn, "sess-1", str(tmp_path))
    db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    resp = client.get("/sessions")

    assert f'/projects/{project["id"]}/decisions' in resp.text
    assert "1 waiting on you" in resp.text


def test_header_has_a_single_search_entry_and_search_page_still_works(db_path):
    client = make_client(db_path)
    html = client.get("/").text
    header = html[html.index("<header"):html.index("</header>")]

    assert 'href="/search"' not in header
    assert header.count("data-palette-open") == 1
    assert ">Search<" in header

    page = client.get("/search?q=anything&deep=1")
    assert page.status_code == 200
    assert "Search transcripts too" in page.text


def _projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / "acme" / ".git").mkdir(parents=True)
    (root / "acme" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "notes").mkdir()
    monkeypatch.setenv("APO_PROJECTS_ROOT", str(root))
    return root


def test_projects_page_has_a_new_project_form_listing_the_roots_folders(db_path, tmp_path, monkeypatch):
    from apo import db

    root = _projects_root(tmp_path, monkeypatch)
    conn = db.get_connection()
    taken = db.get_or_create_project(conn, "Notes App")
    db.create_repository(conn, taken["id"], "notes", str((root / "notes").resolve()))

    text = make_client(db_path).get("/projects").text

    assert 'action="/projects"' in text
    assert 'name="name"' in text
    assert str(root) in text
    assert re.search(r'<option value="acme">acme \(git\)</option>', text)
    assert re.search(r'<option value="notes" disabled>notes — Notes App</option>', text)
    assert '<option value="">No folder</option>' in text


def test_new_project_without_a_folder_opens_its_page(db_path, tmp_path, monkeypatch):
    from apo import db

    _projects_root(tmp_path, monkeypatch)

    resp = make_client(db_path).post("/projects", data={"name": "acme", "folder": ""}, follow_redirects=False)

    project = db.find_project(db.get_connection(), "acme")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/projects/{project['id']}"
    assert db.project_repositories(db.get_connection(), project["id"]) == []


def test_new_project_with_a_folder_attaches_it(db_path, tmp_path, monkeypatch):
    from apo import db

    root = _projects_root(tmp_path, monkeypatch)

    resp = make_client(db_path).post("/projects", data={"name": "Acme", "folder": "acme"})

    project = db.find_project(db.get_connection(), "Acme")
    assert resp.status_code == 200
    assert str((root / "acme").resolve()) in resp.text
    (repo,) = db.project_repositories(db.get_connection(), project["id"])
    assert repo["local_path"] == str((root / "acme").resolve())


def _rejected(db_path, data):
    client = make_client(db_path)
    resp = client.post("/projects", data=data, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/projects?new_project_error=")
    assert resp.headers["location"].endswith("#new-project")
    return client.get(resp.headers["location"]).text


def test_new_project_needs_a_name(db_path, tmp_path, monkeypatch):
    from apo import db

    _projects_root(tmp_path, monkeypatch)

    text = _rejected(db_path, {"name": "  ", "folder": "acme"})

    assert 'class="error-text" role="alert"' in text
    assert '<details id="new-project" class="surface report-bug disclosure" open>' in text
    assert db.get_connection().execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_new_project_refuses_a_name_already_in_use(db_path, tmp_path, monkeypatch):
    from apo import db

    _projects_root(tmp_path, monkeypatch)
    db.get_or_create_project(db.get_connection(), "acme")

    text = _rejected(db_path, {"name": "Acme", "folder": ""})

    assert "already a project called acme" in text
    assert db.get_connection().execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_new_project_refuses_folders_outside_the_root(db_path, tmp_path, monkeypatch):
    from apo import db

    root = _projects_root(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "sneaky").symlink_to(outside, target_is_directory=True)

    for folder in (str(outside), "../elsewhere", "sneaky"):
        _rejected(db_path, {"name": "acme", "folder": folder})
    assert db.find_project(db.get_connection(), "acme") is None


def test_new_project_refuses_a_folder_another_project_has(db_path, tmp_path, monkeypatch):
    from apo import db

    root = _projects_root(tmp_path, monkeypatch)
    conn = db.get_connection()
    other = db.get_or_create_project(conn, "other")
    db.create_repository(conn, other["id"], "other", str((root / "acme").resolve()))

    text = _rejected(db_path, {"name": "acme", "folder": "acme"})

    assert "other" in text
    assert db.find_project(db.get_connection(), "acme") is None
