"""Goals planned in Linear.

An issue you create in the project's Linear project (the one with its name, unless one
was picked on its Overview) becomes a goal there, and the sub-issues you add become the
goal's steps. A step added here becomes a sub-issue in turn. Only issues created by the
Linear account Switchboard connects as come in: an imported step can be work for an
agent, so nobody else's text gets to write one. Runs on the dashboard's poll loop
through sources.LinearSource.sync_goals: only while the Linear connector is on, and only
for projects that have it turned on (db.set_linear_goals).

Status crosses over on a change, never on a level, and each change only once:
- A step remembers the last Linear state it saw and acts only when that changes, and its
  close (done or cancelled) is squared with Linear once (linear_steps.closed_synced_at),
  whichever side closed it first.
- A goal remembers its issue's last state the same way, and the status here last squared
  with Linear (outcomes.linear_status_synced); reopening it here clears that, so closing
  it again is sent again.
So a goal reopened here isn't closed again by its still-completed issue, an issue or
sub-issue reopened in Linear isn't closed again from here, a closed state in Linear is
never overridden, and what Switchboard does because of a change in Linear (the open
agent tasks it cancels when the issue closes) isn't echoed back to Linear.
"""
import sqlite3
from datetime import datetime, timezone

from apo import db
from apo.integrations import linear_client

ERROR_SOURCE = "linear_goals"
# Switchboard's own decision issues carry this prefix (linear_client.create_issue).
# The id check alone misses one whose push reached Linear but isn't saved here yet.
_OWN_TITLE_PREFIX = "[AI] "
_CLOSED = ("completed", "canceled")
_GOAL_STATUS_FOR = {"completed": "achieved", "canceled": "dropped"}
_STATE_FOR_GOAL = {"achieved": "completed", "dropped": "canceled"}
_OPEN_GOAL = ("active", "paused")
# Step kinds whose move is yours: as sub-issues they're assigned to your Linear account.
_YOUR_KINDS = ("action", "decision", "review")


def sync() -> None:
    """Runs inside asyncio.to_thread, so it opens its own connection."""
    conn = db.get_connection()
    started = _now()
    failed = False
    for project in db.linear_goal_projects(conn):
        try:
            import_new_issues(conn, project)
        except Exception as exc:  # noqa: BLE001
            failed = True
            db.record_sync_error(conn, ERROR_SOURCE, None, f"Looking for new issues in {project['name']}: {exc}")
    for goal in _goals_to_sync(conn):
        try:
            sync_goal(conn, goal)
        except Exception as exc:  # noqa: BLE001
            failed = True
            db.record_sync_error(conn, ERROR_SOURCE, None, f"Goal #{goal['id']} ({goal['linear_identifier']}): {exc}")
    if not failed:
        db.resolve_sync_errors(conn, [ERROR_SOURCE], before=started)


def import_new_issues(conn: sqlite3.Connection, project: dict) -> None:
    linear_project_id = project["linear_project_id"] or _remember_name_match(conn, project)
    if linear_project_id is None:
        return
    issues = linear_client.list_new_issues(linear_project_id, project["linear_goals_since"])
    for issue in issues:
        if not _is_new_goal(conn, issue):
            continue
        try:
            db.create_outcome(
                conn, project["id"], issue["title"], description=(issue.get("description") or "").strip() or None,
                linear_issue={"id": issue["id"], "identifier": issue["identifier"], "url": issue["url"],
                              "state": issue["state"]["type"]},
            )
        except sqlite3.IntegrityError:
            conn.rollback()
    if issues:
        # Only forward, and not at all if it was turned off meanwhile (NULL < x is NULL).
        newest = issues[-1]["createdAt"]
        conn.execute(
            "UPDATE projects SET linear_goals_since = ? WHERE id = ? AND linear_goals_since < ?",
            (newest, project["id"], newest),
        )
        conn.commit()


def sync_goal(conn: sqlite3.Connection, goal: dict) -> None:
    issue = linear_client.get_issue_with_children(goal["linear_issue_id"])
    if issue is None:
        return
    links = {r["linear_issue_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM linear_steps WHERE outcome_id = ?", (goal["id"],))}
    for child in issue["children"]:
        link = links.get(child["id"]) or _import_step(conn, goal, child)
        if link:
            _follow_step(conn, link, child["state"]["type"])
    _add_sub_issues(conn, goal, issue)
    _report_closed_steps(conn, goal["id"], {c["id"]: c for c in issue["children"]})
    _follow_goal(conn, goal, issue["state"]["type"])
    _report_goal(conn, goal["id"], issue)


def step_owner(issue: dict) -> tuple[str, str | None]:
    """Who does a sub-issue, as (owner, waiting_on). A label says so outright: "agent",
    or "me"/"human". Otherwise Linear's assignee does: you, someone else (a wait on
    them), or nobody, which leaves it to an agent."""
    labels = {name.strip().lower() for name in issue.get("labels") or []}
    if "agent" in labels:
        return "agent", None
    if labels & {"me", "human"}:
        return "me", None
    assignee = issue.get("assignee")
    if not assignee:
        return "agent", None
    if assignee.get("isMe"):
        return "me", None
    return "external", assignee.get("name") or "someone in Linear"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remember_name_match(conn: sqlite3.Connection, project: dict) -> str | None:
    """The default is the Linear project with this project's name. Once found it's kept,
    so renaming the project here doesn't lose it."""
    linear_project_id = linear_client.find_project_id(linear_client.get_team_id(), project["name"])
    if linear_project_id:
        conn.execute(
            "UPDATE projects SET linear_project_id = ?, linear_project_name = ? WHERE id = ? AND linear_project_id IS NULL",
            (linear_project_id, project["name"], project["id"]),
        )
        conn.commit()
    return linear_project_id


def _yours(issue: dict) -> bool:
    return bool((issue.get("creator") or {}).get("isMe"))


def _is_new_goal(conn: sqlite3.Connection, issue: dict) -> bool:
    """You filed it, it's still open, and Switchboard didn't file it itself (its own
    issues are filed as you too, through your API key)."""
    if not _yours(issue) or issue["state"]["type"] in _CLOSED:
        return False
    if issue["title"].startswith(_OWN_TITLE_PREFIX) or _filed_here(conn, issue["id"]):
        return False
    return not _is_goal(conn, issue["id"])


def _filed_here(conn: sqlite3.Connection, issue_id: str) -> bool:
    return conn.execute("SELECT 1 FROM decision_requests WHERE linear_issue_id = ?", (issue_id,)).fetchone() is not None


def _is_goal(conn: sqlite3.Connection, issue_id: str) -> bool:
    return conn.execute("SELECT 1 FROM outcomes WHERE linear_issue_id = ?", (issue_id,)).fetchone() is not None


def _goals_to_sync(conn: sqlite3.Connection) -> list[dict]:
    """Open goals in projects that have it on, plus a closed one with something Linear
    hasn't been told yet: its own achieve or drop, or a step closed here."""
    return [dict(r) for r in conn.execute(
        "SELECT outcomes.* FROM outcomes JOIN projects ON projects.id = outcomes.project_id "
        "WHERE outcomes.linear_issue_id IS NOT NULL AND projects.linear_goals_since IS NOT NULL "
        "AND projects.archived_at IS NULL AND (outcomes.status IN ('active', 'paused') "
        "OR COALESCE(outcomes.linear_status_synced, '') != outcomes.status OR EXISTS ("
        "SELECT 1 FROM linear_steps JOIN decision_requests ON decision_requests.id = linear_steps.step_id "
        "WHERE linear_steps.outcome_id = outcomes.id AND linear_steps.closed_synced_at IS NULL "
        "AND decision_requests.status IN ('answered', 'resolved', 'cancelled'))) ORDER BY outcomes.id"
    )]


def _link_step(conn: sqlite3.Connection, step_id: int, outcome_id: int, issue: dict, state: str | None) -> dict:
    conn.execute(
        "INSERT INTO linear_steps (step_id, outcome_id, linear_issue_id, linear_identifier, linear_url, linear_state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (step_id, outcome_id, issue["id"], issue["identifier"], issue["url"], state),
    )
    conn.commit()
    return {"step_id": step_id, "linear_state": state}


def _import_step(conn: sqlite3.Connection, goal: dict, child: dict) -> dict | None:
    """A canceled sub-issue isn't a step yet; if it's reopened it's seen as new then."""
    if not _yours(child) or child["state"]["type"] == "canceled":
        return None
    if _filed_here(conn, child["id"]) or _is_goal(conn, child["id"]):
        return None
    unsent = _unsent_steps(conn, goal["id"], title=child["title"])
    if unsent:
        return _link_step(conn, unsent[0], goal["id"], child, None)
    owner, waiting_on = step_owner(child)
    description = (child.get("description") or "").strip()
    link = f"From Linear: {child['identifier']} {child['url']}"
    step_id = db.add_step(
        conn, goal["id"], child["title"], owner, detail=f"{description}\n\n{link}" if description else link,
        waiting_on=waiting_on, repository_id=db.repository_for_new_item(conn, goal["project_id"], None),
    )
    return _link_step(conn, step_id, goal["id"], child, None)


def _follow_step(conn: sqlite3.Connection, link: dict, state: str) -> None:
    if state == link["linear_state"]:
        return
    if state == "completed":
        db.complete_step(conn, link["step_id"], by="linear")
    elif state == "canceled":
        db.cancel_request(conn, link["step_id"], "Canceled in Linear")
    conn.execute(
        "UPDATE linear_steps SET linear_state = ?, closed_synced_at = CASE WHEN ? IN ('completed', 'canceled') "
        "THEN COALESCE(closed_synced_at, ?) ELSE closed_synced_at END WHERE step_id = ?",
        (state, state, _now(), link["step_id"]),
    )
    conn.commit()


def _add_sub_issues(conn: sqlite3.Connection, goal: dict, issue: dict) -> None:
    """Open steps added here become sub-issues of the goal's issue while both are open.
    An agent's own decisions and actions are left out: they already go to Linear as
    issues of their own (sources.LinearSource)."""
    if goal["status"] not in _OPEN_GOAL or issue["state"]["type"] in _CLOSED:
        return
    for step_id in _unsent_steps(conn, goal["id"]):
        step = db.get_decision_request(conn, step_id)
        assignee_id = linear_client.get_viewer_id() if step["kind"] in _YOUR_KINDS else None
        created = linear_client.create_sub_issue(issue, step["title"], _sub_issue_description(step), assignee_id)
        _link_step(conn, step["id"], goal["id"], created, created["state"]["type"])


def _sub_issue_description(step: dict) -> str | None:
    """A wait says who it waits on: that's free text ("Apple review", "the client"), so
    it's written out rather than guessed into a Linear assignee."""
    description = step["question"] if step["question"] != step["title"] else ""
    waiting = f"Waiting on {step['waiting_on']}" if step["kind"] == "wait" and step["waiting_on"] else None
    if waiting and not description.startswith(waiting):
        description = f"{waiting}." + (f"\n\n{description}" if description else "")
    return description or None


def _unsent_steps(conn: sqlite3.Connection, outcome_id: int, title: str | None = None) -> list[int]:
    """Open steps added here that have no sub-issue yet. With a title: the one a sub-issue
    of that name was made for, if Linear took it but saving the link here failed. Adopting
    it beats importing it as a second step that would be sent back as another."""
    query = (
        "SELECT id FROM decision_requests WHERE outcome_id = ? AND status IN ('open', 'in_progress') "
        "AND linear_issue_id IS NULL AND NOT (kind IN ('decision', 'action') AND requested_by = 'agent') "
        "AND id NOT IN (SELECT step_id FROM linear_steps)"
    )
    params: list = [outcome_id]
    if title is not None:
        query += " AND title = ?"
        params.append(title)
    return [r[0] for r in conn.execute(query + " ORDER BY sort_order, id", params)]


def _report_closed_steps(conn: sqlite3.Connection, outcome_id: int, children: dict[str, dict]) -> None:
    """Steps done or cancelled here move their sub-issue to completed or canceled, once.
    One that's left the goal, or was closed in Linear meanwhile, is squared without
    overriding Linear."""
    pending = conn.execute(
        "SELECT linear_steps.step_id, linear_steps.linear_issue_id, decision_requests.status FROM linear_steps "
        "JOIN decision_requests ON decision_requests.id = linear_steps.step_id "
        "WHERE linear_steps.outcome_id = ? AND linear_steps.closed_synced_at IS NULL "
        "AND decision_requests.status IN ('answered', 'resolved', 'cancelled')",
        (outcome_id,),
    ).fetchall()
    for row in pending:
        child = children.get(row["linear_issue_id"])
        state = "canceled" if row["status"] == "cancelled" else "completed"
        told = child is not None and child["state"]["type"] not in _CLOSED
        if told:
            linear_client.move_to_state(row["linear_issue_id"], state, team_id=(child.get("team") or {}).get("id"))
        conn.execute(
            "UPDATE linear_steps SET closed_synced_at = ?, linear_state = CASE WHEN ? THEN ? ELSE linear_state END "
            "WHERE step_id = ?",
            (_now(), told, state, row["step_id"]),
        )
        conn.commit()


def _follow_goal(conn: sqlite3.Connection, goal: dict, state: str) -> None:
    """The issue closing in Linear closes an open goal and cancels its open agent tasks,
    so no agent picks up work for a finished goal. Your own steps and waits stay."""
    if state == goal["linear_state"]:
        return
    status = _GOAL_STATUS_FOR.get(state)
    if status:
        if goal["status"] in _OPEN_GOAL:
            db.set_outcome_status(conn, goal["id"], status, by="linear", cancel_open_steps=False)
        _cancel_agent_tasks(conn, goal["id"])
    conn.execute("UPDATE outcomes SET linear_state = ? WHERE id = ?", (state, goal["id"]))
    conn.commit()


def _cancel_agent_tasks(conn: sqlite3.Connection, outcome_id: int) -> None:
    """Squared on the spot: the close came from Linear, so it isn't sent back there."""
    for row in conn.execute(
        "SELECT id FROM decision_requests WHERE outcome_id = ? AND kind = 'task' AND status IN ('open', 'in_progress')",
        (outcome_id,),
    ).fetchall():
        db.cancel_request(conn, row["id"], "Its goal was closed in Linear")
        conn.execute(
            "UPDATE linear_steps SET closed_synced_at = COALESCE(closed_synced_at, ?) WHERE step_id = ?",
            (_now(), row["id"]),
        )
        conn.commit()


def _report_goal(conn: sqlite3.Connection, outcome_id: int, issue: dict) -> None:
    """A goal achieved or dropped here moves its issue to completed or canceled, once.
    Reopening it here clears that, so closing it again is sent again."""
    goal = db.get_outcome(conn, outcome_id)
    if goal["status"] in _OPEN_GOAL:
        if goal["linear_status_synced"] is not None:
            conn.execute("UPDATE outcomes SET linear_status_synced = NULL WHERE id = ?", (outcome_id,))
            conn.commit()
        return
    if goal["status"] == goal["linear_status_synced"]:
        return
    state = _STATE_FOR_GOAL[goal["status"]]
    told = issue["state"]["type"] not in _CLOSED
    if told:
        linear_client.move_to_state(issue["id"], state, team_id=(issue.get("team") or {}).get("id"))
    conn.execute(
        "UPDATE outcomes SET linear_status_synced = ?, linear_state = CASE WHEN ? THEN ? ELSE linear_state END "
        "WHERE id = ?",
        (goal["status"], told, state, outcome_id),
    )
    conn.commit()
