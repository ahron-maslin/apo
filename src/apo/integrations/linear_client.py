"""Minimal Linear GraphQL client: create an issue per decision request, read comments
back, and move issues between workflow states. move_to_state is general because more
transitions than "-> completed" are plausible (a future phase may want "-> in
progress" once a decision is picked up, "-> blocked" if its work stalls)."""
import json
import urllib.error
import urllib.request

from apo import config
from apo import credentials
from apo import db

API_URL = "https://api.linear.app/graphql"

# Recognized workflow state *types* (vs. an exact state *name*, which varies by
# workspace) — see get_state_id.
_STATE_TYPES = {"triage", "backlog", "unstarted", "started", "completed", "canceled"}


class LinearNotConfigured(Exception):
    pass


def _resolve_credentials() -> tuple[str | None, str | None]:
    """A credential entered through Settings (stored encrypted in the DB) takes
    precedence over APO_LINEAR_API_KEY/APO_LINEAR_TEAM from the environment or the
    settings file, so an existing env-based setup keeps working unchanged until it's
    replaced through the UI."""
    conn = db.get_connection()
    stored = credentials.get_credentials(conn, "linear")
    if stored:
        return stored.get("api_key"), stored.get("team")
    return config.linear_api_key(), config.linear_team()


def _graphql(query: str, variables: dict, api_key: str | None = None) -> dict:
    if config.offline():
        raise RuntimeError(
            "APO_OFFLINE is set — refusing to call the live Linear API. Unset it only "
            "if you actually intend to write to the real workspace."
        )
    if api_key is None:
        api_key, _ = _resolve_credentials()
    if not api_key:
        raise LinearNotConfigured("APO_LINEAR_API_KEY not set")
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Linear API error {exc.code}: {exc.read().decode(errors='replace')}") from exc
    if "errors" in payload:
        raise RuntimeError(f"Linear API error: {payload['errors']}")
    return payload["data"]


def validate_credentials(api_key: str, team: str) -> dict:
    """Confirm a typed-in key works and the typed-in team resolves, before
    credentials.py ever stores them — the Settings UI's connect/test validation
    step. Takes explicit api_key/team (a candidate the user just typed) rather than
    reading config/DB, unlike every other function here. Also says which workspace
    (organization) the key belongs to, so a reconnect can tell a move to another one."""
    data = _graphql(
        "query { viewer { name organization { id name } } teams { nodes { id key name } } }", {}, api_key=api_key,
    )
    team_node = next(
        (t for t in data["teams"]["nodes"] if t["key"] == team or t["id"] == team), None,
    )
    if team_node is None:
        raise RuntimeError(f"No Linear team matching '{team}'")
    result = {
        "display_name": f"{data['viewer']['name']} → {team_node['name']}",
        "team_id": team_node["id"],
    }
    organization = data["viewer"].get("organization")
    if organization:
        result["workspace"] = {"id": organization["id"], "name": organization["name"]}
    return result


_team_id_cache: str | None = None
_viewer_id_cache: str | None = None
_project_id_cache: dict[str, str] = {}
_state_id_cache: dict[tuple[str, str], str] = {}


def clear_caches() -> None:
    """Credentials changing through Settings (a different team, a different
    workspace, or disconnecting back to an env-configured one) must invalidate
    every id resolved under the old credential — otherwise a push/poll after a
    reconnect silently keeps using a team/project/state id that belonged to
    whichever credential was active the first time each cache was populated."""
    global _team_id_cache, _viewer_id_cache
    _team_id_cache = None
    _viewer_id_cache = None
    _project_id_cache.clear()
    _state_id_cache.clear()


def get_team_id() -> str:
    global _team_id_cache
    if _team_id_cache:
        return _team_id_cache
    _, team = _resolve_credentials()
    if not team:
        raise LinearNotConfigured("APO_LINEAR_TEAM not set")
    data = _graphql("query { teams { nodes { id key name } } }", {})
    for node in data["teams"]["nodes"]:
        if node["key"] == team or node["id"] == team:
            _team_id_cache = node["id"]
            return _team_id_cache
    raise RuntimeError(f"No Linear team matching APO_LINEAR_TEAM='{team}'")


def get_viewer_id() -> str:
    """The Linear account the API key belongs to: you, as far as Linear knows."""
    global _viewer_id_cache
    if _viewer_id_cache is None:
        _viewer_id_cache = _graphql("query { viewer { id } }", {})["viewer"]["id"]
    return _viewer_id_cache


def list_projects(team_id: str) -> list[dict]:
    """The team's Linear projects ({id, name}), by name."""
    data = _graphql(
        "query($teamId: String!) { team(id: $teamId) { projects(first: 250) { nodes { id name } } } }",
        {"teamId": team_id},
    )
    return sorted(data["team"]["projects"]["nodes"], key=lambda proj: proj["name"].casefold())


def find_project_id(team_id: str, project_name: str) -> str | None:
    """The team's Linear project with exactly this name, or None. A miss isn't cached,
    so a project someone creates later is found on the next look."""
    if project_name in _project_id_cache:
        return _project_id_cache[project_name]
    for proj in list_projects(team_id):
        if proj["name"] == project_name:
            _project_id_cache[project_name] = proj["id"]
            return proj["id"]
    return None


def get_or_create_project_id(team_id: str, project_name: str) -> str:
    project_id = find_project_id(team_id, project_name)
    if project_id:
        return project_id
    data = _graphql(
        """
        mutation ProjectCreate($input: ProjectCreateInput!) {
            projectCreate(input: $input) {
                success
                project { id name }
            }
        }
        """,
        {"input": {"name": project_name, "teamIds": [team_id]}},
    )
    project_id = data["projectCreate"]["project"]["id"]
    _project_id_cache[project_name] = project_id
    return project_id


def _format_list(label: str, items: list | None) -> str:
    if not items:
        return ""
    body = "\n".join(f"- {item}" for item in items)
    return f"\n\n**{label}:**\n{body}"


def create_issue(project_name: str, decision: dict) -> dict:
    """Create a Linear issue for a decision request, with the full structured
    context, not just question and context."""
    team_id = get_team_id()
    project_id = get_or_create_project_id(team_id, project_name)
    title, description = _issue_text(decision)

    query = """
        mutation IssueCreate($input: IssueCreateInput!) {
            issueCreate(input: $input) {
                success
                issue { id identifier url }
            }
        }
    """
    data = _graphql(query, {"input": {
        "teamId": team_id, "projectId": project_id, "title": title, "description": description,
    }})
    return data["issueCreate"]["issue"]


def update_issue(issue_id: str, decision: dict) -> None:
    """Rewrite an existing issue's title and body from the decision's current text."""
    title, description = _issue_text(decision)
    _graphql(
        """
        mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
            issueUpdate(id: $id, input: $input) { success }
        }
        """,
        {"id": issue_id, "input": {"title": title, "description": description}},
    )


def _issue_text(decision: dict) -> tuple[str, str]:
    title = f"[AI] {decision['title']}"[:255]
    description = ""
    if decision.get("summary"):
        description += f"{decision['summary']}\n\n"
    description += f"**Question:** {decision['question']}"
    if decision.get("context"):
        description += f"\n\n**Context:** {decision['context']}"
    if decision.get("why_it_matters"):
        description += f"\n\n**Why it matters:** {decision['why_it_matters']}"
    if decision.get("options"):
        labels = [o.get("label", "") if isinstance(o, dict) else str(o) for o in decision["options"]]
        description += _format_list("Suggested options", labels)
    if decision.get("recommendation"):
        confidence = f" ({decision['confidence']}% confidence)" if decision.get("confidence") else ""
        description += f"\n\n**Recommendation:** {decision['recommendation']}{confidence}"
    description += _format_list("Blocked work", decision.get("blocked_work"))
    description += _format_list("Safe remaining work", decision.get("safe_work"))
    plan = decision.get("resume_plan")
    if plan and plan.get("remaining_steps"):
        description += _format_list("Resume plan", plan["remaining_steps"])
    description += (
        "\n\n---\nReply with a comment on this issue to answer — it syncs back to "
        "apo automatically and lands in the repo for the next session to "
        "pick up."
    )
    return title, description


def get_state_id(team_id: str, type_or_name: str) -> str:
    """Resolve a workflow state by type (e.g. "completed") or, if not a recognized
    type keyword, by exact name — since state names vary by workspace but types are
    a fixed Linear vocabulary."""
    cache_key = (team_id, type_or_name)
    if cache_key in _state_id_cache:
        return _state_id_cache[cache_key]
    data = _graphql(
        "query($teamId: String!) { team(id: $teamId) { states { nodes { id name type } } } }",
        {"teamId": team_id},
    )
    states = data["team"]["states"]["nodes"]
    if type_or_name in _STATE_TYPES:
        state = next((s for s in states if s["type"] == type_or_name), None)
    else:
        state = next((s for s in states if s["name"] == type_or_name), None)
    if state is None:
        raise RuntimeError(f"No workflow state matching '{type_or_name}' found for team {team_id}")
    _state_id_cache[cache_key] = state["id"]
    return state["id"]


def move_to_state(issue_id: str, type_or_name: str, team_id: str | None = None) -> None:
    """team_id: the issue's own team, when it may not be the one decisions are filed in."""
    state_id = get_state_id(team_id or get_team_id(), type_or_name)
    _graphql(
        """
        mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
            issueUpdate(id: $id, input: $input) { success }
        }
        """,
        {"id": issue_id, "input": {"stateId": state_id}},
    )


def create_comment(issue_id: str, body: str) -> None:
    _graphql(
        """
        mutation CommentCreate($input: CommentCreateInput!) {
            commentCreate(input: $input) { success }
        }
        """,
        {"input": {"issueId": issue_id, "body": body}},
    )


def get_comments(issue_id: str) -> list[dict]:
    query = """
        query IssueComments($issueId: String!) {
            issue(id: $issueId) {
                comments(orderBy: createdAt) {
                    nodes { id body createdAt user { name } }
                }
            }
        }
    """
    data = _graphql(query, {"issueId": issue_id})
    issue = data.get("issue")
    if not issue:
        return []
    return issue["comments"]["nodes"]


# Bounds one look for new issues. The cursor only trails the poll by APO_POLL_SECONDS, so
# more than this many new issues in one project between polls isn't a real case.
_NEW_ISSUE_PAGES = 10


def list_new_issues(project_id: str, since: str) -> list[dict]:
    """Top-level issues created in a Linear project after `since`, oldest first. The
    filter goes in as one IssueFilter variable, so nothing depends on the name of the
    comparator's scalar type."""
    query = """
        query NewIssues($filter: IssueFilter, $after: String) {
            issues(filter: $filter, first: 50, after: $after) {
                nodes { id identifier url title description createdAt creator { name isMe } state { type } }
                pageInfo { hasNextPage endCursor }
            }
        }
    """
    issue_filter = {"project": {"id": {"eq": project_id}}, "createdAt": {"gt": since}, "parent": {"null": True}}
    issues, after = [], None
    for _ in range(_NEW_ISSUE_PAGES):
        page = _graphql(query, {"filter": issue_filter, "after": after})["issues"]
        issues.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return sorted(issues, key=lambda issue: issue["createdAt"])


def get_issue_with_children(issue_id: str) -> dict | None:
    """An issue's state, team and project, and its sub-issues (oldest first, label names
    flattened)."""
    query = """
        query GoalIssue($id: String!) {
            issue(id: $id) {
                id state { type } team { id } project { id }
                children(first: 100) {
                    nodes {
                        id identifier url title description createdAt state { type } team { id }
                        creator { isMe } assignee { name isMe } labels(first: 20) { nodes { name } }
                    }
                }
            }
        }
    """
    issue = _graphql(query, {"id": issue_id}).get("issue")
    if not issue:
        return None
    children = sorted(issue["children"]["nodes"], key=lambda child: child["createdAt"])
    for child in children:
        child["labels"] = [label["name"] for label in child["labels"]["nodes"]]
    return {**issue, "children": children}


def create_sub_issue(parent: dict, title: str, description: str | None, assignee_id: str | None = None) -> dict:
    """A sub-issue of `parent` (as get_issue_with_children returns it), in the parent's
    own team and project."""
    issue_input = {"teamId": parent["team"]["id"], "parentId": parent["id"]}
    if parent.get("project"):
        issue_input["projectId"] = parent["project"]["id"]
    issue_input["title"] = title[:255]
    if description:
        issue_input["description"] = description
    if assignee_id:
        issue_input["assigneeId"] = assignee_id
    data = _graphql(
        """
        mutation SubIssueCreate($input: IssueCreateInput!) {
            issueCreate(input: $input) {
                success
                issue { id identifier url state { type } }
            }
        }
        """,
        {"input": issue_input},
    )
    return data["issueCreate"]["issue"]
