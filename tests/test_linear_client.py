import pytest


def test_graphql_raises_when_not_configured(linear_client_module):
    with pytest.raises(linear_client_module.LinearNotConfigured):
        linear_client_module._graphql("query {}", {})


def test_get_team_id_matches_by_key(linear_client_module, config_module, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")
    monkeypatch.setenv("LINEAR_TEAM", "ENG")
    linear_client_module.__dict__["_team_id_cache"] = None
    def fake_graphql(query, variables):
        return {"teams": {"nodes": [{"id": "team-1", "key": "ENG", "name": "Engineering"}]}}
    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    assert linear_client_module.get_team_id() == "team-1"


def test_create_issue_includes_structured_sections(linear_client_module, config_module, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")
    monkeypatch.setenv("LINEAR_TEAM", "ENG")
    monkeypatch.setattr(linear_client_module, "get_team_id", lambda: "team-1")
    monkeypatch.setattr(linear_client_module, "get_or_create_project_id", lambda team_id, name: "proj-1")

    captured = {}

    def fake_graphql(query, variables):
        captured["query"] = query
        captured["variables"] = variables
        return {"issueCreate": {"success": True, "issue": {
            "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/issue/ENG-1",
        }}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    decision = {
        "title": "Clarify mobile navigation",
        "question": "Bottom tabs, drawer, or sidebar?",
        "context": "Desktop done.",
        "why_it_matters": "Only mobile nav depends on this.",
        "options": [{"label": "Bottom tabs"}, {"label": "Drawer"}],
        "recommendation": "Drawer navigation.",
        "confidence": 72,
        "blocked_work": ["MobileMenu.tsx"],
        "safe_work": ["Backend"],
        "resume_plan": {"remaining_steps": ["Implement drawer"]},
    }
    issue = linear_client_module.create_issue("acme", decision)

    assert issue["identifier"] == "ENG-1"
    description = captured["variables"]["input"]["description"]
    assert "Bottom tabs" in description
    assert "Drawer navigation." in description
    assert "MobileMenu.tsx" in description
    assert "Implement drawer" in description
    assert captured["variables"]["input"]["title"] == "[AI] Clarify mobile navigation"


def test_create_issue_handles_plain_string_options(linear_client_module, config_module, monkeypatch):
    """create_issue must not assume every caller already normalized options into
    {label, detail} dicts — db.get_decision_request does that for the normal path,
    but create_issue is a public function other callers/tests can reach directly,
    and it previously crashed with `'str' object has no attribute 'get'` on a plain
    list of strings."""
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")
    monkeypatch.setenv("LINEAR_TEAM", "ENG")
    monkeypatch.setattr(linear_client_module, "get_team_id", lambda: "team-1")
    monkeypatch.setattr(linear_client_module, "get_or_create_project_id", lambda team_id, name: "proj-1")

    captured = {}

    def fake_graphql(query, variables):
        captured["variables"] = variables
        return {"issueCreate": {"success": True, "issue": {
            "id": "iss-1", "identifier": "ENG-1", "url": "https://linear.app/x/issue/ENG-1",
        }}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    decision = {
        "title": "Clarify mobile navigation",
        "question": "Bottom tabs, drawer, or sidebar?",
        "options": ["Bottom tabs", "Drawer"],
    }
    issue = linear_client_module.create_issue("acme", decision)

    assert issue["identifier"] == "ENG-1"
    description = captured["variables"]["input"]["description"]
    assert "Bottom tabs" in description
    assert "Drawer" in description


def test_move_to_state_resolves_by_type(linear_client_module, monkeypatch):
    monkeypatch.setattr(linear_client_module, "get_team_id", lambda: "team-1")
    calls = []

    def fake_graphql(query, variables):
        if "states" in query:
            return {"team": {"states": {"nodes": [{"id": "state-done", "name": "Done", "type": "completed"}]}}}
        calls.append(variables)
        return {"issueUpdate": {"success": True}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)
    linear_client_module.move_to_state("iss-1", "completed")

    assert calls[0]["input"]["stateId"] == "state-done"


def test_get_comments_returns_empty_for_missing_issue(linear_client_module, monkeypatch):
    monkeypatch.setattr(linear_client_module, "_graphql", lambda q, v: {"issue": None})
    assert linear_client_module.get_comments("missing") == []


def test_create_comment_posts_issue_id_and_body(linear_client_module, monkeypatch):
    calls = []

    def fake_graphql(query, variables):
        calls.append(variables)
        return {"commentCreate": {"success": True}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)
    linear_client_module.create_comment("iss-1", "The answer text")

    assert calls[0]["input"]["issueId"] == "iss-1"
    assert calls[0]["input"]["body"] == "The answer text"


def test_ado_offline_blocks_graphql_entirely(monkeypatch, linear_client_module):
    """Hard backstop for manual/scratch scripts, which run outside conftest's
    isolation and load the real .env. Blocking at _graphql (not just at
    is_configured) is deliberate: it also catches a script calling linear_client
    directly, which is exactly how four junk issues once reached the live workspace
    while verifying an unrelated feature."""
    import pytest

    monkeypatch.setenv("ADO_OFFLINE", "1")

    with pytest.raises(RuntimeError, match="ADO_OFFLINE"):
        linear_client_module._graphql("query { teams { nodes { id } } }", {})


def test_linear_source_reports_unconfigured_when_offline(monkeypatch, sources_module, config_module):
    monkeypatch.setenv("LINEAR_API_KEY", "real-looking-key")
    monkeypatch.setenv("ADO_OFFLINE", "1")

    assert sources_module.LinearSource().is_configured() is False


def test_resolve_credentials_falls_back_to_env_when_no_db_credential(linear_client_module, config_module, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "env-fallback-key")
    monkeypatch.setenv("LINEAR_TEAM", "ENV_TEAM")

    assert linear_client_module._resolve_credentials() == ("env-fallback-key", "ENV_TEAM")


def test_resolve_credentials_prefers_db_credential_over_env_fallback(
    db_path, linear_client_module, config_module, credentials_module
, monkeypatch):
    from apo import db

    monkeypatch.setenv("LINEAR_API_KEY", "env-fallback-key")
    monkeypatch.setenv("LINEAR_TEAM", "ENV_TEAM")
    conn = db.get_connection()
    credentials_module.set_credentials(
        conn, "linear", {"api_key": "db-key", "team": "DB_TEAM"}, display_name="x"
    )

    assert linear_client_module._resolve_credentials() == ("db-key", "DB_TEAM")


def test_graphql_raises_not_configured_when_neither_db_nor_env_has_a_key(linear_client_module):
    with pytest.raises(linear_client_module.LinearNotConfigured):
        linear_client_module._graphql("query {}", {})


def test_graphql_accepts_explicit_api_key_override_bypassing_resolution(monkeypatch, linear_client_module):
    import json as json_module
    import urllib.request

    captured = {}

    class FakeResponse:
        def read(self):
            return json_module.dumps({"data": {"ok": True}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=15):
        captured["authorization"] = req.get_header("Authorization")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = linear_client_module._graphql("query {}", {}, api_key="explicit-key")

    assert result == {"ok": True}
    assert captured["authorization"] == "explicit-key"


def test_graphql_sends_db_resolved_api_key_as_authorization_header(
    db_path, monkeypatch, linear_client_module, config_module, credentials_module
):
    from apo import db
    import json as json_module
    import urllib.request

    monkeypatch.setenv("LINEAR_API_KEY", "env-fallback-key")
    conn = db.get_connection()
    credentials_module.set_credentials(conn, "linear", {"api_key": "db-key", "team": "DB_TEAM"}, display_name="x")

    captured = {}

    class FakeResponse:
        def read(self):
            return json_module.dumps({"data": {"ok": True}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=15):
        captured["authorization"] = req.get_header("Authorization")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    linear_client_module._graphql("query {}", {})

    assert captured["authorization"] == "db-key"


def test_validate_credentials_returns_display_name_and_resolved_team_id(linear_client_module, monkeypatch):
    def fake_graphql(query, variables, api_key=None):
        assert api_key == "typed-key"
        return {
            "viewer": {"name": "Test User"},
            "teams": {"nodes": [{"id": "team-1", "key": "ENG", "name": "Engineering"}]},
        }

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    result = linear_client_module.validate_credentials("typed-key", "ENG")

    assert result == {"display_name": "Test User → Engineering", "team_id": "team-1"}


def test_validate_credentials_says_which_workspace_the_key_belongs_to(linear_client_module, monkeypatch):
    def fake_graphql(query, variables, api_key=None):
        assert "organization { id name }" in query
        return {
            "viewer": {"name": "Test User", "organization": {"id": "org-1", "name": "Example Workspace"}},
            "teams": {"nodes": [{"id": "team-1", "key": "ENG", "name": "Engineering"}]},
        }

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    result = linear_client_module.validate_credentials("typed-key", "ENG")

    assert result["workspace"] == {"id": "org-1", "name": "Example Workspace"}


def test_validate_credentials_matches_team_by_id_too(linear_client_module, monkeypatch):
    def fake_graphql(query, variables, api_key=None):
        return {
            "viewer": {"name": "Test User"},
            "teams": {"nodes": [{"id": "team-1", "key": "ENG", "name": "Engineering"}]},
        }

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    result = linear_client_module.validate_credentials("typed-key", "team-1")

    assert result["team_id"] == "team-1"


def test_validate_credentials_raises_when_team_not_found(linear_client_module, monkeypatch):
    def fake_graphql(query, variables, api_key=None):
        return {
            "viewer": {"name": "Test User"},
            "teams": {"nodes": [{"id": "team-1", "key": "ENG", "name": "Engineering"}]},
        }

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    with pytest.raises(RuntimeError, match="No Linear team matching"):
        linear_client_module.validate_credentials("typed-key", "NOPE")


def test_validate_credentials_propagates_graphql_errors(linear_client_module, monkeypatch):
    def fake_graphql(query, variables, api_key=None):
        raise RuntimeError("Linear API error 401: Unauthorized")

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    with pytest.raises(RuntimeError, match="401"):
        linear_client_module.validate_credentials("bad-key", "ENG")


def test_finding_a_project_by_name_never_creates_one(linear_client_module, monkeypatch):
    """Polling for new issues must never make a Linear project appear."""
    queries = []

    def fake_graphql(query, variables):
        queries.append(query)
        return {"team": {"projects": {"nodes": [{"id": "proj-1", "name": "acme"}]}}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    assert linear_client_module.find_project_id("team-1", "acme") == "proj-1"
    assert linear_client_module.find_project_id("team-1", "globex") is None
    assert not any("projectCreate" in q for q in queries)


def test_new_issues_are_the_projects_top_level_ones_since_the_cursor_oldest_first(linear_client_module, monkeypatch):
    pages = [
        {"issues": {"nodes": [{"id": "later", "createdAt": "2026-10-18T12:00:02.000Z"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "page-2"}}},
        {"issues": {"nodes": [{"id": "earlier", "createdAt": "2026-10-18T12:00:01.000Z"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None}}},
    ]
    calls = []

    def fake_graphql(query, variables):
        calls.append(variables)
        return pages[len(calls) - 1]

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    issues = linear_client_module.list_new_issues("proj-1", "2026-10-18T12:00:00.000Z")

    assert [i["id"] for i in issues] == ["earlier", "later"]
    assert calls[0]["filter"] == {
        "project": {"id": {"eq": "proj-1"}},
        "createdAt": {"gt": "2026-10-18T12:00:00.000Z"},
        "parent": {"null": True},
    }
    assert calls[0]["after"] is None and calls[1]["after"] == "page-2"


def test_a_goal_issue_comes_back_with_its_sub_issues_and_their_label_names(linear_client_module, monkeypatch):
    def fake_graphql(query, variables):
        assert variables == {"id": "iss-1"}
        return {"issue": {"id": "iss-1", "state": {"type": "started"}, "children": {"nodes": [
            {"id": "c-2", "createdAt": "2026-10-18T12:00:02.000Z", "labels": {"nodes": [{"name": "Agent"}]}},
            {"id": "c-1", "createdAt": "2026-10-18T12:00:01.000Z", "labels": {"nodes": []}},
        ]}}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    issue = linear_client_module.get_issue_with_children("iss-1")

    assert issue["state"] == {"type": "started"}
    assert [c["id"] for c in issue["children"]] == ["c-1", "c-2"]
    assert issue["children"][1]["labels"] == ["Agent"]


def test_a_missing_goal_issue_comes_back_as_none(linear_client_module, monkeypatch):
    monkeypatch.setattr(linear_client_module, "_graphql", lambda query, variables: {"issue": None})

    assert linear_client_module.get_issue_with_children("gone") is None


def test_moving_an_issue_can_use_its_own_teams_states(linear_client_module, monkeypatch):
    """A sub-issue can live in another team than the one Switchboard files into, and
    a state id from the wrong team is refused."""
    monkeypatch.setattr(linear_client_module, "get_team_id", lambda: "team-1")
    calls = []

    def fake_graphql(query, variables):
        calls.append(variables)
        if "states" in query:
            return {"team": {"states": {"nodes": [{"id": f"{variables['teamId']}-done", "name": "Done", "type": "completed"}]}}}
        return {"issueUpdate": {"success": True}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    linear_client_module.move_to_state("iss-9", "completed", team_id="team-2")

    assert calls[0] == {"teamId": "team-2"}
    assert calls[1] == {"id": "iss-9", "input": {"stateId": "team-2-done"}}


def test_the_teams_projects_are_listed_by_name_well_past_one_default_page(linear_client_module, monkeypatch):
    queries = []

    def fake_graphql(query, variables):
        queries.append(query)
        return {"team": {"projects": {"nodes": [{"id": "p-2", "name": "globex"}, {"id": "p-1", "name": "Acme"}]}}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    assert linear_client_module.list_projects("team-1") == [{"id": "p-1", "name": "Acme"}, {"id": "p-2", "name": "globex"}]
    assert "projects(first: 250)" in queries[0]


def test_a_sub_issue_is_created_under_its_parent_in_the_parents_team_and_project(linear_client_module, monkeypatch):
    captured = {}

    def fake_graphql(query, variables):
        captured.update(query=query, variables=variables)
        return {"issueCreate": {"success": True, "issue": {
            "id": "iss-9", "identifier": "ENG-9", "url": "https://linear.app/x/issue/ENG-9", "state": {"type": "backlog"}}}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)
    parent = {"id": "iss-1", "team": {"id": "team-2"}, "project": {"id": "proj-1"}}

    issue = linear_client_module.create_sub_issue(parent, "Order the boxes", "Kraft, 20x15.")

    assert issue["identifier"] == "ENG-9" and issue["state"] == {"type": "backlog"}
    assert captured["variables"]["input"] == {
        "teamId": "team-2", "parentId": "iss-1", "projectId": "proj-1",
        "title": "Order the boxes", "description": "Kraft, 20x15.",
    }


def test_a_sub_issue_without_a_project_or_description_leaves_them_out(linear_client_module, monkeypatch):
    captured = {}

    def fake_graphql(query, variables):
        captured.update(variables=variables)
        return {"issueCreate": {"success": True, "issue": {"id": "iss-9", "identifier": "ENG-9", "url": "u",
                                                            "state": {"type": "backlog"}}}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    linear_client_module.create_sub_issue({"id": "iss-1", "team": {"id": "team-2"}, "project": None}, "Order", None)

    assert captured["variables"]["input"] == {"teamId": "team-2", "parentId": "iss-1", "title": "Order"}


def test_goal_issues_say_who_created_each_sub_issue(linear_client_module, monkeypatch):
    captured = {}

    def fake_graphql(query, variables):
        captured["query"] = query
        return {"issue": None}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)
    linear_client_module.get_issue_with_children("iss-1")

    assert "creator { isMe }" in captured["query"] and "project { id }" in captured["query"]


def test_the_viewer_is_looked_up_once_and_forgotten_with_the_other_caches(linear_client_module, monkeypatch):
    calls = []

    def fake_graphql(query, variables):
        calls.append(query)
        return {"viewer": {"id": "user-1"}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    assert linear_client_module.get_viewer_id() == "user-1"
    assert linear_client_module.get_viewer_id() == "user-1"
    assert len(calls) == 1

    linear_client_module.clear_caches()
    linear_client_module.get_viewer_id()
    assert len(calls) == 2


def test_a_sub_issue_can_be_assigned(linear_client_module, monkeypatch):
    captured = {}

    def fake_graphql(query, variables):
        captured.update(variables=variables)
        return {"issueCreate": {"success": True, "issue": {"id": "iss-9", "identifier": "ENG-9", "url": "u",
                                                            "state": {"type": "backlog"}}}}

    monkeypatch.setattr(linear_client_module, "_graphql", fake_graphql)

    linear_client_module.create_sub_issue({"id": "iss-1", "team": {"id": "team-2"}, "project": None}, "Order", None,
                                          assignee_id="user-1")

    assert captured["variables"]["input"]["assigneeId"] == "user-1"
