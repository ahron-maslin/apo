"""Goals planned in Linear. An issue you create in the project's Linear project (the one
with its name, unless you picked another) becomes a goal there, and its sub-issues
become the goal's steps; a step added here becomes a sub-issue. Done, cancelled,
achieved and dropped cross over both ways, but only on a change: neither side's update
is ever bounced back to the other. It's a per-project switch, off by default, and only
your own Linear account's issues come in, because an imported step can be work for an
agent."""
import os
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apo import db


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class FakeLinear:
    """Just enough of a Linear workspace: issues in projects, sub-issues and states."""

    def __init__(self):
        self.projects = {"acme": "lp-acme", "Shop": "lp-shop"}
        self.issues = {}
        self.moves = []
        self.created = []
        self.viewer_lookups = 0
        self.reads = 0
        self._clock = datetime.now(timezone.utc) + timedelta(seconds=1)

    def issue(self, title, parent=None, state="unstarted", description=None, creator="me",
              assignee=None, labels=(), created=None, project="lp-acme"):
        """creator: "me" (the API key's own account), another person's name, or None
        (an integration)."""
        n = len(self.issues) + 1
        self._clock += timedelta(seconds=1)
        issue = {
            "id": f"iss-{n}", "identifier": f"ENG-{n}", "url": f"https://linear.app/x/issue/ENG-{n}",
            "title": title, "description": description, "createdAt": created or _stamp(self._clock),
            "creator": creator and {"name": "Test User" if creator == "me" else creator, "isMe": creator == "me"},
            "state": {"type": state}, "assignee": assignee, "labels": list(labels),
            "parent": parent and parent["id"], "project_id": project, "team": {"id": "team-1"},
        }
        self.issues[issue["id"]] = issue
        return issue

    def find_project_id(self, team_id, name):
        return self.projects.get(name)

    def list_projects(self, team_id):
        return [{"id": pid, "name": name} for name, pid in self.projects.items()]

    def list_new_issues(self, project_id, since):
        self.reads += 1
        return sorted(
            (i for i in self.issues.values()
             if i["project_id"] == project_id and i["parent"] is None and i["createdAt"] > since),
            key=lambda i: i["createdAt"],
        )

    def get_issue_with_children(self, issue_id):
        self.reads += 1
        issue = self.issues.get(issue_id)
        if issue is None:
            return None
        children = sorted((i for i in self.issues.values() if i["parent"] == issue_id), key=lambda i: i["createdAt"])
        return {**issue, "project": {"id": issue["project_id"]}, "children": [dict(c) for c in children]}

    def get_viewer_id(self):
        self.viewer_lookups += 1
        return "viewer-1"

    def create_sub_issue(self, parent, title, description, assignee_id=None):
        assert parent["team"] == {"id": "team-1"} and parent["project"] == {"id": self.issues[parent["id"]]["project_id"]}
        assert assignee_id in (None, "viewer-1")
        child = self.issue(title, parent=parent, description=description, project=parent["project"]["id"],
                           assignee=assignee_id and {"name": "Test User", "isMe": True})
        self.created.append(child["id"])
        return {k: child[k] for k in ("id", "identifier", "url", "state")}

    def move_to_state(self, issue_id, state, team_id=None):
        assert team_id == self.issues[issue_id]["team"]["id"]
        self.moves.append((issue_id, state))
        self.issues[issue_id]["state"] = {"type": state}


@pytest.fixture
def linear(db_path, monkeypatch):
    from apo.integrations import linear_client

    fake = FakeLinear()
    monkeypatch.setattr(linear_client, "get_team_id", lambda: "team-1")
    monkeypatch.setattr(linear_client, "find_project_id", fake.find_project_id)
    monkeypatch.setattr(linear_client, "list_new_issues", fake.list_new_issues)
    monkeypatch.setattr(linear_client, "get_issue_with_children", fake.get_issue_with_children)
    monkeypatch.setattr(linear_client, "move_to_state", fake.move_to_state)
    monkeypatch.setattr(linear_client, "list_projects", fake.list_projects)
    monkeypatch.setattr(linear_client, "create_sub_issue", fake.create_sub_issue)
    monkeypatch.setattr(linear_client, "get_viewer_id", fake.get_viewer_id)
    return fake


@pytest.fixture
def acme(linear, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    (tmp_path / "acme").mkdir()
    db.create_repository(conn, project["id"], "acme", str(tmp_path / "acme"))
    db.set_linear_goals(conn, project["id"], True)
    return dict(project)


@pytest.fixture
def client(db_path):
    from apo.web import app

    return TestClient(app.app)


def sync():
    from apo.integrations import linear_goals

    linear_goals.sync()


def _goal(issue):
    row = db.get_connection().execute("SELECT id FROM outcomes WHERE linear_issue_id = ?", (issue["id"],)).fetchone()
    return db.get_outcome(db.get_connection(), row["id"]) if row else None


def _steps(goal):
    return db.outcome_progress(db.get_connection(), goal["id"])["steps"]


def _goal_count():
    return db.get_connection().execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]


# --- The switch ---

def test_turning_it_on_starts_from_now_and_turning_it_off_forgets_where_it_was(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    since = lambda: conn.execute("SELECT linear_goals_since FROM projects WHERE id = ?", (project["id"],)).fetchone()[0]
    assert since() is None

    before = _stamp(datetime.now(timezone.utc))
    db.set_linear_goals(conn, project["id"], True)
    started = since()
    assert started >= before

    db.set_linear_goals(conn, project["id"], True)
    assert since() == started

    db.set_linear_goals(conn, project["id"], False)
    assert since() is None


def test_nothing_is_imported_or_even_read_while_it_is_off(linear, acme):
    db.set_linear_goals(db.get_connection(), acme["id"], False)
    linear.issue("Launch the listing")

    sync()

    assert _goal_count() == 0
    assert linear.reads == 0


# --- Issues become goals ---

def test_an_issue_someone_creates_in_linear_becomes_a_goal_in_its_project(linear, acme):
    issue = linear.issue("Launch the listing", description="Etsy first, then Amazon.")

    sync()

    goal = _goal(issue)
    assert goal["project_id"] == acme["id"] and goal["status"] == "active"
    assert goal["title"] == "Launch the listing"
    assert goal["description"] == "Etsy first, then Amazon."
    assert (goal["linear_issue_id"], goal["linear_identifier"], goal["linear_url"]) == (
        issue["id"], "ENG-1", "https://linear.app/x/issue/ENG-1")


def test_each_issue_is_imported_once(linear, acme):
    linear.issue("Launch the listing")

    sync()
    sync()
    conn = db.get_connection()
    conn.execute("UPDATE projects SET linear_goals_since = '2000-01-01T00:00:00.000Z'")
    conn.commit()
    sync()

    assert _goal_count() == 1


def test_issues_filed_before_it_was_turned_on_are_left_alone(linear, acme):
    linear.issue("Something from last year", created="2025-10-01T09:00:00.000Z")

    sync()

    assert _goal_count() == 0


def test_issues_control_center_filed_itself_are_not_goals(linear, acme):
    conn = db.get_connection()
    decision_id = db.create_decision_request(conn, project_id=acme["id"], title="Which nav?", question="Tabs?")
    pushed = linear.issue("[AI] Which nav?")
    conn.execute("UPDATE decision_requests SET linear_issue_id = ? WHERE id = ?", (pushed["id"], decision_id))
    conn.commit()
    linear.issue("[AI] A push whose issue id isn't saved yet")

    sync()

    assert _goal_count() == 0


def test_only_open_issues_you_created_become_goals(linear, acme):
    linear.issue("Filed by an integration", creator=None)
    linear.issue("Filed by a teammate", creator="Dana")
    linear.issue("Already done", state="completed")
    linear.issue("Never mind", state="canceled")
    wanted = linear.issue("Launch the listing", state="triage")

    sync()

    assert _goal_count() == 1
    assert _goal(wanted) is not None


# --- Sub-issues become steps ---

def test_sub_issues_become_steps_including_ones_added_later(linear, acme):
    parent = linear.issue("Launch the listing")
    linear.issue("Write the copy", parent=parent)

    sync()
    linear.issue("Set the price", parent=parent)
    sync()
    sync()

    assert [s["title"] for s in _steps(_goal(parent))] == ["Write the copy", "Set the price"]


def test_an_unassigned_sub_issue_is_a_ready_task_for_an_agent_with_its_brief_and_link(linear, acme, tmp_path):
    parent = linear.issue("Launch the listing")
    child = linear.issue("Write the copy", parent=parent, description="Use the brand voice.")

    sync()

    (step,) = _steps(_goal(parent))
    assert step["owner"] == "agent" and step["status"] == "open"
    assert "Use the brand voice." in step["question"] and child["url"] in step["question"]
    assert step["linear_step"] == {"identifier": "ENG-2", "url": child["url"]}
    index = (tmp_path / "acme" / ".claude" / "decisions" / "INDEX.md").read_text()
    assert re.search(r"^\| READY \| .*Write the copy", index, re.M)


def test_the_assignee_or_a_label_decides_who_does_a_step(linear, acme):
    parent = linear.issue("Launch the listing")
    me, dana = {"name": "Test User", "isMe": True}, {"name": "Dana", "isMe": False}
    linear.issue("Nobody's yet", parent=parent)
    linear.issue("Assigned to me", parent=parent, assignee=me)
    linear.issue("Assigned to Dana", parent=parent, assignee=dana)
    linear.issue("Mine, but labelled for an agent", parent=parent, assignee=me, labels=["Agent"])
    linear.issue("Labelled for me", parent=parent, labels=["human"])

    sync()

    steps = _steps(_goal(parent))
    assert [s["owner"] for s in steps] == ["agent", "me", "external", "agent", "me"]
    assert steps[2]["waiting_on"] == "Dana"


def test_a_canceled_sub_issue_is_skipped_and_a_finished_one_arrives_done(linear, acme):
    parent = linear.issue("Launch the listing")
    linear.issue("Dropped idea", parent=parent, state="canceled")
    linear.issue("Already written", parent=parent, state="completed")

    sync()

    (step,) = _steps(_goal(parent))
    assert step["title"] == "Already written" and step["done"]
    assert linear.moves == []


def test_a_sub_issue_that_is_itself_a_goal_is_not_also_a_step(linear, acme):
    parent = linear.issue("Launch the listing")
    other = linear.issue("Open a shop")
    sync()

    other["parent"] = parent["id"]
    sync()

    assert _steps(_goal(parent)) == []


# --- Status, both ways ---

def test_a_sub_issue_completed_in_linear_marks_its_step_done_without_echoing_back(linear, acme):
    parent = linear.issue("Launch the listing")
    child = linear.issue("Write the copy", parent=parent)
    sync()

    child["state"] = {"type": "completed"}
    sync()
    sync()

    (step,) = _steps(_goal(parent))
    assert step["done"] and step["answer_source"] == "linear"
    assert linear.moves == []


@pytest.mark.parametrize("assignee", [None, {"name": "Test User", "isMe": True}], ids=["agent step", "your step"])
def test_a_step_done_here_moves_its_sub_issue_to_completed_once(linear, acme, assignee):
    parent = linear.issue("Launch the listing")
    child = linear.issue("Write the copy", parent=parent, assignee=assignee)
    sync()
    (step,) = _steps(_goal(parent))

    db.complete_step(db.get_connection(), step["id"], by="agent" if assignee is None else "human")
    sync()
    sync()

    assert linear.moves == [(child["id"], "completed")]


def test_a_sub_issue_reopened_in_linear_is_not_closed_again(linear, acme):
    parent = linear.issue("Launch the listing")
    child = linear.issue("Write the copy", parent=parent)
    sync()
    (step,) = _steps(_goal(parent))
    db.complete_task(db.get_connection(), step["id"], note="Written")
    sync()

    child["state"] = {"type": "started"}
    sync()
    sync()

    assert linear.moves == [(child["id"], "completed")]
    assert _steps(_goal(parent))[0]["done"]


def test_a_sub_issue_canceled_in_linear_cancels_its_step(linear, acme):
    parent = linear.issue("Launch the listing")
    child = linear.issue("Write the copy", parent=parent)
    sync()

    child["state"] = {"type": "canceled"}
    sync()

    (step,) = _steps(_goal(parent))
    assert step["status"] == "cancelled"
    assert linear.moves == []


@pytest.mark.parametrize("state, status", [("completed", "achieved"), ("canceled", "dropped")])
def test_the_parent_issue_closing_in_linear_closes_the_goal(linear, acme, state, status):
    parent = linear.issue("Launch the listing")
    linear.issue("Write the copy", parent=parent)
    sync()

    parent["state"] = {"type": state}
    sync()

    goal = _goal(parent)
    assert goal["status"] == status
    if status == "dropped":
        assert _steps(goal)[0]["status"] == "cancelled"
    assert linear.moves == []


def test_a_goal_you_reopen_here_is_not_closed_again_by_linear(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()
    parent["state"] = {"type": "completed"}
    sync()

    db.set_outcome_status(db.get_connection(), _goal(parent)["id"], "active")
    sync()

    assert _goal(parent)["status"] == "active"


def test_a_step_done_just_before_you_close_the_goal_still_reaches_linear(linear, acme):
    parent = linear.issue("Launch the listing")
    child = linear.issue("Write the copy", parent=parent)
    sync()
    goal = _goal(parent)
    conn = db.get_connection()

    db.complete_step(conn, _steps(goal)[0]["id"], by="agent")
    db.set_outcome_status(conn, goal["id"], "achieved")
    sync()

    assert linear.moves == [(child["id"], "completed"), (parent["id"], "completed")]


def test_a_closed_goal_stops_being_read_once_linear_has_been_told(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()
    db.set_outcome_status(db.get_connection(), _goal(parent)["id"], "dropped")
    sync()
    reads = linear.reads

    sync()

    assert linear.reads == reads + 1  # just the look for new issues


# --- Only your own account's issues ---

def test_sub_issues_someone_else_adds_are_not_steps(linear, acme):
    parent = linear.issue("Launch the listing")
    linear.issue("Mine", parent=parent)
    linear.issue("A teammate's", parent=parent, creator="Dana")
    sync()
    linear.issue("A teammate's, added later", parent=parent, creator="Dana")
    linear.issue("By an integration", parent=parent, creator=None)
    sync()

    assert [s["title"] for s in _steps(_goal(parent))] == ["Mine"]


# --- Which Linear project ---

def test_the_name_match_is_remembered_so_a_rename_keeps_importing(linear, acme):
    sync()
    conn = db.get_connection()
    db.rename_project(conn, acme["id"], "Acme Shop")

    issue = linear.issue("Launch the listing")
    sync()

    assert _goal(issue)["project_id"] == acme["id"]
    row = conn.execute("SELECT linear_project_id, linear_project_name FROM projects WHERE id = ?", (acme["id"],)).fetchone()
    assert tuple(row) == ("lp-acme", "acme")


def test_choosing_another_linear_project_imports_from_it_from_then_on(linear, acme):
    conn = db.get_connection()
    conn.execute("UPDATE projects SET linear_goals_since = '2000-01-01T00:00:00.000Z' WHERE id = ?", (acme["id"],))
    conn.commit()
    linear.issue("Filed in Shop before the switch", project="lp-shop", created="2020-01-01T00:00:00.000Z")

    db.set_linear_project(conn, acme["id"], "lp-shop", "Shop")
    from_shop = linear.issue("Launch the listing", project="lp-shop")
    from_acme = linear.issue("Something in the old one", project="lp-acme")
    sync()

    assert _goal(from_shop)["project_id"] == acme["id"]
    assert _goal(from_acme) is None and _goal_count() == 1


def test_the_picker_offers_the_teams_linear_projects_with_the_current_one_chosen(client, linear, acme, config_module, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")
    page = client.get(f"/projects/{acme['id']}").text
    assert "Linear project" in page and f"/projects/{acme['id']}?choose_linear=1" in page

    page = client.get(f"/projects/{acme['id']}?choose_linear=1").text

    picker = re.search(r'<select[^>]*name="linear_project".*?</select>', page, re.S).group(0)
    assert re.search(r'<option value="lp-acme" selected>acme</option>', picker)
    assert '<option value="lp-shop">Shop</option>' in picker


def test_picking_a_linear_project_saves_it(client, linear, acme, config_module, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")

    client.post(f"/projects/{acme['id']}/linear-project", data={"linear_project": "lp-shop"})

    row = db.get_connection().execute(
        "SELECT linear_project_id, linear_project_name FROM projects WHERE id = ?", (acme["id"],)).fetchone()
    assert tuple(row) == ("lp-shop", "Shop")
    assert "Shop" in client.get(f"/projects/{acme['id']}").text


def test_only_one_of_the_teams_linear_projects_can_be_picked(client, linear, acme, config_module, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")

    response = client.post(f"/projects/{acme['id']}/linear-project", data={"linear_project": "someone-elses"})

    assert response.status_code == 400
    row = db.get_connection().execute("SELECT linear_project_id FROM projects WHERE id = ?", (acme["id"],)).fetchone()
    assert row[0] is None


def test_the_picker_says_so_when_linear_cannot_be_reached(client, linear, acme, config_module, monkeypatch):
    from apo.integrations import linear_client

    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")

    def down(team_id):
        raise RuntimeError("Linear API error 503")

    monkeypatch.setattr(linear_client, "list_projects", down)

    page = client.get(f"/projects/{acme['id']}?choose_linear=1")

    assert page.status_code == 200 and "Couldn't load your Linear projects" in page.text


def _stored_linear_project(project_id):
    row = db.get_connection().execute(
        "SELECT linear_project_id, linear_project_name, linear_goals_since FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return tuple(row)


@pytest.fixture
def workspace(linear, monkeypatch):
    """Which Linear workspace the next connect lands in: set .id/.name before connecting."""
    from apo.integrations import linear_client

    class Workspace:
        id, name = "org-1", "Example Workspace"

    def validate(api_key, team):
        return {"display_name": f"Test User → {Workspace.name}", "team_id": "team-1",
                "workspace": {"id": Workspace.id, "name": Workspace.name}}

    monkeypatch.setattr(linear_client, "validate_credentials", validate)
    # Once connected, filing a request pushes it.
    monkeypatch.setattr(linear_client, "create_issue",
                        lambda project_name, decision: {"id": "pushed", "identifier": "P-1", "url": "u"})
    return Workspace


def _connect(client):
    return client.post("/settings/integrations/linear/connect", data={"api_key": "key", "team": "ENG"})


def _linked_goal(linear, acme):
    """A goal imported from Linear with one step, and a Linear goals sync error standing."""
    parent = linear.issue("Launch the listing")
    linear.issue("Write the copy", parent=parent)
    sync()
    conn = db.get_connection()
    db.set_linear_project(conn, acme["id"], "lp-shop", "Shop")
    db.record_sync_error(conn, "linear_goals", None, "Goal #1 (ENG-1): Entity not found: Issue")
    return parent


def _still_linked(parent):
    goal = _goal(parent)
    return goal is not None and db.get_connection().execute(
        "SELECT COUNT(*) FROM linear_steps WHERE outcome_id = ?", (goal["id"],)).fetchone()[0] == 1


def test_the_first_connect_remembers_the_workspace_and_changes_nothing_else(client, linear, acme, workspace):
    parent = _linked_goal(linear, acme)

    _connect(client)

    assert db.get_linear_workspace(db.get_connection()) == {"id": "org-1", "name": "Example Workspace"}
    assert _stored_linear_project(acme["id"])[:2] == ("lp-shop", "Shop")
    assert _still_linked(parent)


def _pushed_request(project_id, issue_id, status="open", **extra):
    """A request Switchboard already pushed to Linear, as LinearSource leaves it."""
    conn = db.get_connection()
    request_id = db.create_decision_request(conn, project_id=project_id, title=f"Ask {issue_id}", question="?")
    columns = {"linear_issue_id": issue_id, "linear_identifier": f"OLD-{issue_id}", "status": status,
               "linear_url": f"https://linear.app/old/issue/{issue_id}", "linear_synced_comment_id": "c-1", **extra}
    conn.execute(f"UPDATE decision_requests SET {', '.join(f'{k} = ?' for k in columns)} WHERE id = ?",
                 (*columns.values(), request_id))
    conn.commit()
    return request_id


_REQUEST_LINK = ("linear_issue_id", "linear_identifier", "linear_url", "linear_synced_comment_id",
                 "linear_resolve_error", "linear_next_retry_at")


def _request_link(request_id):
    row = db.get_connection().execute(
        f"SELECT {', '.join(_REQUEST_LINK)}, linear_retry_attempts FROM decision_requests WHERE id = ?", (request_id,)
    ).fetchone()
    return tuple(row)


def test_reconnecting_to_the_same_workspace_keeps_projects_and_goal_links(client, linear, acme, workspace):
    from apo.integrations import linear_client

    _connect(client)
    parent = _linked_goal(linear, acme)
    request_id = _pushed_request(acme["id"], "old-1")
    since = _stored_linear_project(acme["id"])[2]
    linear_client._project_id_cache["acme"] = "stale"

    _connect(client)

    assert _stored_linear_project(acme["id"]) == ("lp-shop", "Shop", since)
    assert _still_linked(parent)
    assert _request_link(request_id)[0] == "old-1"
    assert linear_client._project_id_cache == {}


def test_connecting_to_another_workspace_unlinks_the_old_ones_goals_but_keeps_them(client, linear, acme, workspace):
    _connect(client)
    parent = _linked_goal(linear, acme)
    conn = db.get_connection()
    other = db.get_or_create_project(conn, "globex")
    db.set_linear_project(conn, other["id"], "lp-globex", "globex")
    goal_id, since = _goal(parent)["id"], _stored_linear_project(acme["id"])[2]

    workspace.id, workspace.name = "org-2", "Globex"
    _connect(client)

    goal = db.get_outcome(conn, goal_id)
    assert goal["title"] == "Launch the listing" and goal["status"] == "active"
    assert [goal[k] for k in ("linear_issue_id", "linear_identifier", "linear_url", "linear_state",
                              "linear_status_synced")] == [None] * 5
    progress = db.outcome_progress(conn, goal_id)
    assert [s["title"] for s in progress["steps"]] == ["Write the copy"] and progress["steps"][0]["linear_step"] is None
    assert conn.execute("SELECT COUNT(*) FROM linear_steps").fetchone()[0] == 0
    stored = _stored_linear_project(acme["id"])
    assert stored[:2] == (None, None) and stored[2] >= since  # still on, restarted
    assert _stored_linear_project(other["id"])[:2] == (None, None)
    assert db.recent_sync_errors(conn, unresolved_only=True) == []
    assert db.get_linear_workspace(conn) == {"id": "org-2", "name": "Globex"}
    events = [e for e in db.list_events(conn, project_id=acme["id"]) if e["verb"] == "project.linear_unlinked"]
    assert len(events) == 1 and "1 goal" in events[0]["summary"] and "Globex" in events[0]["summary"]
    assert not [e for e in db.list_events(conn, project_id=other["id"]) if e["verb"] == "project.linear_unlinked"]

    linear.projects["acme"] = "lp-acme-new"  # the new workspace's own project of that name
    reads = linear.reads
    sync()
    assert linear.reads == reads + 1  # the look for new issues; the unlinked goal isn't read
    assert _goal_count() == 1


def test_moving_workspace_unlinks_the_requests_pushed_to_the_old_one(client, linear, acme, workspace, config_module,
                                                                     monkeypatch):
    from apo.integrations import linear_client

    _connect(client)
    open_id = _pushed_request(acme["id"], "old-1")
    answered_id = _pushed_request(acme["id"], "old-2", status="resolved", answer_text="Tabs.",
                                  resolved_at=datetime.now(timezone.utc).isoformat(),
                                  linear_resolve_error="Entity not found", linear_retry_attempts=2,
                                  linear_next_retry_at="2000-01-01T00:00:00+00:00")
    conn = db.get_connection()
    db.record_sync_error(conn, "linear_poll", open_id, "Entity not found: Issue")
    db.record_sync_error(conn, "linear_resolve", answered_id, "Entity not found: Issue")

    workspace.id, workspace.name = "org-2", "Globex"
    _connect(client)

    for request_id in (open_id, answered_id):
        assert _request_link(request_id) == (None,) * len(_REQUEST_LINK) + (0,)
        assert db.get_decision_request(conn, request_id)["title"].startswith("Ask")
    assert db.recent_sync_errors(conn, unresolved_only=True) == []
    (event,) = [e for e in db.list_events(conn, project_id=acme["id"]) if e["verb"] == "project.linear_unlinked"]
    assert "2 requests unlinked" in event["summary"]

    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")
    calls = []
    for name in ("get_comments", "create_issue", "create_comment"):
        monkeypatch.setattr(linear_client, name, lambda *a, _name=name, **k: calls.append(_name))
    monkeypatch.setattr(linear_client, "move_to_state", lambda *a, **k: calls.append("move_to_state"))
    from apo.web import app as app_module
    import asyncio
    asyncio.run(app_module.poll_sources_once())
    assert calls == []


def test_a_request_whose_push_failed_is_still_retried_into_the_new_workspace(client, linear, acme, workspace,
                                                                          config_module, monkeypatch):
    """Not one of the old workspace's: it never reached Linear, so it's pushed as any
    failed push is, once Linear answers."""
    from apo.integrations import linear_client

    _connect(client)

    def down(project_name, decision):
        raise RuntimeError("timed out")

    monkeypatch.setattr(linear_client, "create_issue", down)
    conn = db.get_connection()
    request_id = db.create_decision_request(conn, project_id=acme["id"], title="Which nav?", question="Tabs?")
    conn.execute("UPDATE decision_requests SET linear_next_retry_at = NULL WHERE id = ?", (request_id,))
    conn.commit()
    assert _request_link(request_id)[0] is None

    workspace.id = "org-2"
    _connect(client)
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")
    monkeypatch.setattr(linear_client, "create_issue",
                        lambda project_name, decision: {"id": "new-1", "identifier": "NEW-1", "url": "u"})
    monkeypatch.setattr(linear_client, "get_comments", lambda issue_id: [])
    from apo.web import app as app_module
    import asyncio
    asyncio.run(app_module.poll_sources_once())

    assert _request_link(request_id)[0] == "new-1"


def test_the_unlinked_event_counts_goals_and_requests(client, linear, acme, workspace):
    _connect(client)
    _linked_goal(linear, acme)
    _pushed_request(acme["id"], "old-1")
    _pushed_request(acme["id"], "old-2")
    conn = db.get_connection()
    other = db.get_or_create_project(conn, "globex")
    _pushed_request(other["id"], "old-3")

    workspace.id, workspace.name = "org-2", "Globex"
    _connect(client)

    def unlinked(project_id):
        return [e["summary"] for e in db.list_events(conn, project_id=project_id) if e["verb"] == "project.linear_unlinked"]

    (acme_event,) = unlinked(acme["id"])
    assert acme_event.startswith("1 goal and 2 requests unlinked from Linear")
    (other_event,) = unlinked(other["id"])
    assert other_event.startswith("1 request unlinked from Linear")


def test_moving_workspace_restarts_the_import_from_that_moment(client, linear, acme, workspace):
    _connect(client)
    conn = db.get_connection()
    conn.execute("UPDATE projects SET linear_goals_since = '2000-01-01T00:00:00.000Z' WHERE id = ?", (acme["id"],))
    off = db.get_or_create_project(conn, "globex")
    conn.commit()

    before = _stamp(datetime.now(timezone.utc))
    workspace.id = "org-2"
    _connect(client)
    linear.issue("Filed in the new workspace last year", created="2025-10-01T09:00:00.000Z")
    sync()

    assert _stored_linear_project(acme["id"])[2] >= before
    assert _stored_linear_project(off["id"])[2] is None
    assert _goal_count() == 0


def test_after_moving_workspace_the_name_match_there_is_used(client, linear, acme, workspace):
    _connect(client)
    db.set_linear_project(db.get_connection(), acme["id"], "lp-shop", "Shop")
    workspace.id = "org-2"
    _connect(client)
    linear.projects["acme"] = "lp-acme-new"

    issue = linear.issue("Launch the listing", project="lp-acme-new")
    sync()

    assert _goal(issue)["project_id"] == acme["id"]
    assert _stored_linear_project(acme["id"])[:2] == ("lp-acme-new", "acme")


def test_disconnecting_and_reconnecting_to_the_same_workspace_loses_nothing(client, linear, acme, workspace):
    _connect(client)
    parent = _linked_goal(linear, acme)

    client.post("/settings/integrations/linear/disconnect")
    assert _stored_linear_project(acme["id"])[:2] == ("lp-shop", "Shop") and _still_linked(parent)
    assert db.get_linear_workspace(db.get_connection())["id"] == "org-1"

    _connect(client)
    assert _stored_linear_project(acme["id"])[:2] == ("lp-shop", "Shop") and _still_linked(parent)


def test_a_connect_that_cannot_say_which_workspace_changes_nothing_stored(client, linear, acme, monkeypatch):
    from apo.integrations import linear_client

    monkeypatch.setattr(linear_client, "validate_credentials",
                        lambda api_key, team: {"display_name": "Test User → Engineering", "team_id": "team-1"})
    db.set_linear_workspace(db.get_connection(), {"id": "org-1", "name": "Example Workspace"})
    parent = _linked_goal(linear, acme)

    _connect(client)

    assert _still_linked(parent) and db.get_linear_workspace(db.get_connection())["id"] == "org-1"


def test_testing_the_connection_keeps_each_projects_linear_project(client, linear, acme, workspace):
    from apo import credentials

    conn = db.get_connection()
    credentials.set_credentials(conn, "linear", {"api_key": "k", "team": "ENG"}, display_name="Test User → Engineering")
    db.set_linear_workspace(conn, {"id": "org-9", "name": "Elsewhere"})
    parent = _linked_goal(linear, acme)

    client.post("/settings/integrations/linear/test")

    assert _stored_linear_project(acme["id"])[:2] == ("lp-shop", "Shop") and _still_linked(parent)
    assert db.get_linear_workspace(conn)["id"] == "org-9"


# --- Steps added here become sub-issues ---

def test_a_step_added_here_becomes_a_sub_issue_of_the_goals_issue(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()
    goal = _goal(parent)

    step_id = db.add_step(db.get_connection(), goal["id"], "Order the boxes", owner="agent", detail="Kraft, 20x15.")
    sync()
    sync()

    (created,) = linear.created
    sub = linear.issues[created]
    assert (sub["parent"], sub["title"], sub["description"]) == (parent["id"], "Order the boxes", "Kraft, 20x15.")
    (step,) = _steps(goal)
    assert step["id"] == step_id and step["linear_step"]["url"] == sub["url"]


def test_only_a_step_you_own_is_assigned_to_you_when_it_becomes_a_sub_issue(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()
    conn = db.get_connection()
    goal_id = _goal(parent)["id"]
    db.add_step(conn, goal_id, "Approve the price", owner="me")
    db.add_step(conn, goal_id, "Write the copy", owner="agent")
    db.add_step(conn, goal_id, "Product photos", owner="external", waiting_on="Dana", detail="Six angles.")

    sync()

    mine, agents, wait = (linear.issues[i] for i in linear.created)
    assert mine["assignee"] == {"name": "Test User", "isMe": True}
    assert agents["assignee"] is None and wait["assignee"] is None
    assert wait["description"] == "Waiting on Dana.\n\nSix angles."
    assert [s["owner"] for s in _steps(_goal(parent))] == ["me", "agent", "external"]


def test_your_linear_account_is_looked_up_only_for_a_step_you_own(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()
    db.add_step(db.get_connection(), _goal(parent)["id"], "Write the copy", owner="agent")

    sync()

    assert linear.viewer_lookups == 0


def test_a_sub_issue_added_from_here_closes_when_its_step_is_done(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()
    step_id = db.add_step(db.get_connection(), _goal(parent)["id"], "Order the boxes", owner="me")
    sync()

    db.complete_step(db.get_connection(), step_id, by="human")
    sync()

    assert linear.moves == [(linear.created[0], "completed")]


def test_a_sub_issue_made_for_a_step_whose_link_was_lost_is_adopted_not_duplicated(linear, acme):
    """Linear took the new sub-issue but saving its link here failed: next time round it
    must not come back as a second step, which would itself be sent as another one."""
    parent = linear.issue("Launch the listing")
    sync()
    step_id = db.add_step(db.get_connection(), _goal(parent)["id"], "Order the boxes", owner="agent")
    orphan = linear.issue("Order the boxes", parent=parent)

    sync()
    sync()

    (step,) = _steps(_goal(parent))
    assert step["id"] == step_id and step["linear_step"]["url"] == orphan["url"]
    assert linear.created == []


def test_an_agents_question_on_the_goal_stays_its_own_issue_not_a_sub_issue(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()

    db.create_decision_request(db.get_connection(), project_id=acme["id"], title="Which boxes?", question="Kraft?",
                               outcome_id=_goal(parent)["id"])
    sync()

    assert linear.created == []


# --- Closing here reaches Linear ---

@pytest.mark.parametrize("status, state", [("achieved", "completed"), ("dropped", "canceled")])
def test_achieving_or_dropping_a_goal_here_closes_its_issue_once(linear, acme, status, state):
    parent = linear.issue("Launch the listing")
    sync()

    db.set_outcome_status(db.get_connection(), _goal(parent)["id"], status)
    sync()
    sync()

    assert linear.moves == [(parent["id"], state)]


def test_a_goal_closed_here_then_reopened_in_linear_is_not_closed_again(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()
    db.set_outcome_status(db.get_connection(), _goal(parent)["id"], "achieved")
    sync()

    parent["state"] = {"type": "started"}
    sync()
    sync()

    assert linear.moves == [(parent["id"], "completed")]


def test_a_goal_reopened_here_and_achieved_again_tells_linear_again(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()
    conn = db.get_connection()
    goal_id = _goal(parent)["id"]
    db.set_outcome_status(conn, goal_id, "achieved")
    sync()
    parent["state"] = {"type": "started"}

    db.set_outcome_status(conn, goal_id, "active")
    sync()
    db.set_outcome_status(conn, goal_id, "achieved")
    sync()

    assert linear.moves == [(parent["id"], "completed"), (parent["id"], "completed")]


def test_closing_a_goal_here_never_overrides_an_issue_already_closed_in_linear(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()

    db.set_outcome_status(db.get_connection(), _goal(parent)["id"], "dropped")
    parent["state"] = {"type": "completed"}
    sync()

    assert linear.moves == []
    assert _goal(parent)["status"] == "dropped"


def test_cancelling_a_step_here_cancels_its_sub_issue_once(linear, acme):
    parent = linear.issue("Launch the listing")
    child = linear.issue("Write the copy", parent=parent)
    sync()

    db.cancel_request(db.get_connection(), _steps(_goal(parent))[0]["id"], "Not needed")
    sync()
    sync()

    assert linear.moves == [(child["id"], "canceled")]


def test_dropping_a_goal_here_cancels_its_open_steps_sub_issues_and_the_issue(linear, acme):
    parent = linear.issue("Launch the listing")
    open_child = linear.issue("Write the copy", parent=parent)
    done_child = linear.issue("Set the price", parent=parent, state="completed")
    sync()

    db.set_outcome_status(db.get_connection(), _goal(parent)["id"], "dropped")
    sync()

    assert linear.moves == [(open_child["id"], "canceled"), (parent["id"], "canceled")]
    assert done_child["state"] == {"type": "completed"}


# --- The issue closing in Linear stops the agents' work on it ---

@pytest.mark.parametrize("state", ["completed", "canceled"])
def test_the_issue_closing_in_linear_cancels_only_the_goals_open_agent_tasks(linear, acme, state):
    parent = linear.issue("Launch the listing")
    linear.issue("Ready for an agent", parent=parent)
    linear.issue("Claimed by an agent", parent=parent)
    linear.issue("Mine", parent=parent, assignee={"name": "Test User", "isMe": True})
    linear.issue("Dana's", parent=parent, assignee={"name": "Dana", "isMe": False})
    sync()
    conn = db.get_connection()
    steps = _steps(_goal(parent))
    db.claim_task(conn, steps[1]["id"], "session-1")

    parent["state"] = {"type": state}
    sync()
    sync()

    assert [s["status"] for s in _steps(_goal(parent))] == ["cancelled", "cancelled", "open", "open"]
    assert linear.moves == []


def test_a_step_added_here_as_the_issue_closes_in_linear_is_not_added_to_it(linear, acme):
    parent = linear.issue("Launch the listing")
    sync()
    db.add_step(db.get_connection(), _goal(parent)["id"], "Order the boxes", owner="agent")

    parent["state"] = {"type": "completed"}
    sync()

    assert linear.created == []
    assert _steps(_goal(parent))[0]["status"] == "cancelled"


# --- Upgrading ---

def test_upgrading_never_closes_issues_for_goals_closed_before_closing_crossed_over(linear, acme):
    parent = linear.issue("Launch the listing")
    child = linear.issue("Write the copy", parent=parent)
    sync()
    conn = db.get_connection()
    goal_id = _goal(parent)["id"]
    db.set_outcome_status(conn, goal_id, "dropped")
    conn.execute("UPDATE outcomes SET linear_status_synced = NULL")
    conn.execute("UPDATE linear_steps SET closed_synced_at = NULL")
    conn.execute("ALTER TABLE linear_steps RENAME COLUMN closed_synced_at TO done_synced_at")
    conn.execute("PRAGMA user_version = 17")
    conn.commit()
    conn.close()

    sync()

    assert linear.moves == []
    columns = [r[1] for r in db.get_connection().execute("PRAGMA table_info(linear_steps)")]
    assert "closed_synced_at" in columns and "done_synced_at" not in columns


# --- Failures ---

def test_a_failed_read_is_a_sync_error_that_clears_once_linear_answers_again(linear, acme, monkeypatch):
    from apo.integrations import linear_client

    parent = linear.issue("Launch the listing")
    sync()

    def down(issue_id):
        raise RuntimeError("Linear API error 503")

    monkeypatch.setattr(linear_client, "get_issue_with_children", down)
    sync()
    conn = db.get_connection()
    (error,) = db.recent_sync_errors(conn, unresolved_only=True)
    assert error["source"] == "linear_goals" and "ENG-1" in error["message"] and "503" in error["message"]

    monkeypatch.setattr(linear_client, "get_issue_with_children", linear.get_issue_with_children)
    sync()
    assert db.recent_sync_errors(conn, unresolved_only=True) == []


async def test_the_poll_loop_brings_goals_in_only_while_linear_sync_is_on(linear, acme, config_module, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")
    monkeypatch.setenv("LINEAR_TEAM", "ENG")
    conn = db.get_connection()
    db.set_connector_enabled(conn, "linear", False)
    linear.issue("Launch the listing")

    from apo.web import app as app_module
    await app_module.poll_sources_once()
    assert _goal_count() == 0

    db.set_connector_enabled(conn, "linear", True)
    await app_module.poll_sources_once()
    assert _goal_count() == 1


# --- Where you see it ---

def test_the_project_page_turns_goals_from_linear_on_and_off(client, acme, config_module, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")
    page = client.get(f"/projects/{acme['id']}").text
    assert "Goals from Linear" in page and "Turn off" in page

    client.post(f"/projects/{acme['id']}/linear-goals", data={"enabled": "0"})

    row = db.get_connection().execute("SELECT linear_goals_since FROM projects WHERE id = ?", (acme["id"],)).fetchone()
    assert row[0] is None
    assert "Turn on" in client.get(f"/projects/{acme['id']}").text


def test_the_project_page_says_to_connect_linear_first(client, acme):
    db.set_linear_goals(db.get_connection(), acme["id"], False)

    page = client.get(f"/projects/{acme['id']}").text

    assert "Connect Linear" in page and 'action="/projects/%d/linear-goals"' % acme["id"] not in page


def test_a_project_left_on_while_linear_is_disconnected_can_still_be_turned_off(client, acme):
    page = client.get(f"/projects/{acme['id']}").text

    assert "Turn off" in page and "nothing comes in until" in page


def test_settings_lists_the_projects_that_take_goals_from_linear(client, acme, config_module, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key")

    page = client.get("/settings").text

    assert re.search(r"Goals from Linear.*acme", page, re.S)


def test_the_goal_page_and_goal_card_link_back_to_the_issue(client, linear, acme):
    parent = linear.issue("Launch the listing", description="Etsy first.\n<b>Then</b> Amazon.")
    child = linear.issue("Write the copy", parent=parent)
    sync()
    goal = _goal(parent)

    page = client.get(f"/outcomes/{goal['id']}").text
    assert parent["url"] in page and "ENG-1" in page
    assert "Etsy first." in page and "&lt;b&gt;Then&lt;/b&gt;" in page and "<b>Then</b>" not in page
    assert child["url"] in page

    for cards in (client.get(f"/projects/{acme['id']}").text, client.get("/projects").text):
        card = re.search(r'<div class="outcome-card.*?</div>\s*</div>', cards, re.S).group(0)
        assert f'href="{parent["url"]}"' in card and f'href="/outcomes/{goal["id"]}"' in card


def test_a_goal_without_linear_shows_no_linear_link(client, acme):
    conn = db.get_connection()
    outcome_id = db.create_outcome(conn, acme["id"], "Launch the listing")

    page = client.get(f"/outcomes/{outcome_id}").text

    assert "linear.app" not in page and "Open in Linear" not in page


def test_agents_see_the_goals_description_and_link(linear, acme):
    from apo import mcp_server

    parent = linear.issue("Launch the listing", description="Etsy first.")
    sync()

    (goal,) = mcp_server.list_outcomes("acme")
    assert goal["description"] == "Etsy first." and goal["linear_url"] == parent["url"]
