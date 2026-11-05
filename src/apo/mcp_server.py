"""MCP server — the primary agent-facing write path.

Thin wrapper over db.py, same rule sources.py's AnswerSource already enforces for
answer channels: this is a protocol adapter, not a second source of truth. Tool
functions stay plain, testable callables (FastMCP's @mcp.tool() returns the original
function unmodified) so they're covered by tests/test_mcp_server.py without needing a
live MCP transport.
"""
import os

from mcp.server.fastmcp import FastMCP

from apo import claude_runtime
from apo import db

INSTRUCTIONS = """\
apo is where the human running many projects and sessions sees what needs them. Use it instead of stopping, or asking in chat, when you need the human, and to keep shared work visible.

Work from the project's folder: a session's folder is its project, and answers land in that folder's .claude/decisions/. If you were resumed from another folder, work there with absolute paths.

Before planning, read the project's brief (get_project_brief) and its goals (list_outcomes). The brief includes the project's standing rules, also in .claude/decisions/POLICY.md: answers the human gives every time. If a rule answers your question, follow it instead of filing, and say which rule you followed.

When you need the human:
- A decision (choosing between options): create_decision_request. Give a two-sentence summary they can read in five seconds; options, each with its consequence and whether it's reversible; your recommendation and confidence; what's blocked vs. safe to continue; and a resume_plan, the full handoff for whoever applies the answer later. Then keep going on the safe work.
- Something only they can do (DNS, merging, access, payment): create_action_request, with where (a link), steps, values to copy, inputs you need back (mark secret ones and read them later with get_action_secret; never write them into files), done_when, and a verify check (dns, http, file, env or pr_merged) when you can.
- No longer needed (you found the answer, the plan changed): withdraw_request.
- Answered in this chat: record it with answer_decision_request, so the dashboard, Linear and the repo stay in step.
- Before asking, check list_open_questions and list_resolved_questions: it may already be asked or decided.

When an answer arrives you're told on your next prompt, or it shows as ANSWERED in .claude/decisions/INDEX.md. Do the work, then mark_decision_resolved. The user runs /decisions to work answers in bulk; never run it yourself unless asked. OPEN items are still with the human: never act on or re-ask them.

Multi-step results: create_outcome starts a goal with steps, each owned by "agent", "me" (the human) or "external"; add_step adds more. Agent steps are tasks: list_tasks shows them; claim_task before starting one and complete_task, with a note, when it's done. If a task is stuck or failed, retry_task puts it back to ready. set_task_blockers changes what a task waits on. Only work tasks when the user asks. Each item has a notes thread: read the "## Notes" section of its file in .claude/decisions/ (the user's notes appear there), and add_note leaves one back. If those tools are missing in this session (it predates them), use the CLI instead: apo claim <id>, apo complete <id> --note "...".

Your own checklist: keep your TaskCreate / TodoWrite list current. apo mirrors it to the dashboard, so the human sees your progress without asking; no tool call needed.

register_repository only when filing for a folder apo doesn't know yet. Never put a password, key or token in a request or answer: they're written into the repo.
"""

mcp = FastMCP("apo", instructions=INSTRUCTIONS)


def _origin_session_id(explicit: str | None) -> str | None:
    """This server is spawned once per Claude Code process and outlives /clear and
    /resume, so CLAUDE_CODE_SESSION_ID in its env names whichever session started it,
    not the one calling now. Ask the parent process's live record first."""
    return explicit or claude_runtime.current_session_id() or os.environ.get("CLAUDE_CODE_SESSION_ID")


def _resolve_filing(conn, project: str, repository: str | None):
    """The folder the server runs in identifies the project (see
    db.resolve_project_for_filing); an explicit repository name still wins."""
    proj, repository_id = db.resolve_project_for_filing(conn, project, os.getcwd())
    if repository:
        repo = conn.execute(
            "SELECT * FROM repositories WHERE project_id = ? AND name = ?", (proj["id"], repository),
        ).fetchone()
        if repo:
            repository_id = repo["id"]
    return proj, repository_id


@mcp.tool()
def create_decision_request(
    project: str,
    title: str,
    question: str,
    summary: str | None = None,
    context: str | None = None,
    why_it_matters: str | None = None,
    options: list | None = None,
    blocked_work: list | None = None,
    safe_work: list | None = None,
    recommendation: str | None = None,
    confidence: int | None = None,
    resume_plan: dict | None = None,
    repository: str | None = None,
    session_id: str | None = None,
    recommended_option: int | None = None,
    supersedes: int | None = None,
) -> dict:
    """File a decision request: something blocked, plus what's safe to keep working on.

    supersedes: the id of an earlier request this one replaces (a refined
    re-ask, "round 2"); that one is cancelled and linked to this.

    summary is what the human sees first, on the dashboard and in Linear — one or two
    plain sentences they can read in five seconds to know what's being asked, no jargon
    or file paths. Put everything the resuming agent needs (context, why_it_matters,
    resume_plan, etc.) in the other fields; those stay full detail for whoever picks
    the work up, they just aren't the first thing a human reads.

    options become one-click buttons for the human. Each can be a plain string or
    {label, detail, consequence, reversible}: consequence is what you will do if it's
    picked ("add a compose service and migration 0007"), reversible whether that's
    easy to undo. recommended_option is the 0-based index of the one you recommend;
    the human can accept it in one click.

    Fill in resume_plan properly — it is the entire handoff. Whoever picks this up
    after it's answered is a fresh session (usually a subagent) reading the generated
    `.claude/decisions/NNNN-*.md`, with none of your current context.

    session_id records which session asked (defaults to the CLAUDE_CODE_SESSION_ID env
    var Claude Code sets, so callers don't normally pass it) — provenance only.
    """
    conn = db.get_connection()
    proj, repository_id = _resolve_filing(conn, project, repository)
    decision_id = db.create_decision_request(
        conn,
        project_id=proj["id"],
        repository_id=repository_id,
        title=title,
        question=question,
        summary=summary,
        context=context,
        why_it_matters=why_it_matters,
        options=options,
        blocked_work=blocked_work,
        safe_work=safe_work,
        recommendation=recommendation,
        confidence=confidence,
        recommended_option=recommended_option,
        resume_plan=resume_plan,
        origin_session_id=_origin_session_id(session_id),
        supersedes_id=supersedes,
    )
    return {"id": decision_id}


@mcp.tool()
def create_action_request(
    project: str,
    title: str,
    question: str,
    summary: str | None = None,
    context: str | None = None,
    why_it_matters: str | None = None,
    blocked_work: list | None = None,
    safe_work: list | None = None,
    resume_plan: dict | None = None,
    repository: str | None = None,
    session_id: str | None = None,
    where: str | None = None,
    steps: list | None = None,
    values: list | None = None,
    inputs: list | None = None,
    done_when: str | None = None,
    verify: dict | None = None,
    est_minutes: int | None = None,
) -> dict:
    """File an Action: something only a human can physically go do — grant a
    credential, click a button in a third-party console, merge a PR, plug something
    in. Use this instead of create_decision_request when what's missing isn't your
    judgment, it's your hands: there's no option to choose between, just something
    that needs doing before work can continue. Keep doing whatever is in safe_work as
    long as there is any; file this once nothing is left until the action is done.

    Same fields as create_decision_request minus options/recommendation/confidence,
    which don't apply to an Action. Resolve it the same way (answer_decision_request
    or mark_decision_resolved) — the human's answer is a "here's what I did"
    confirmation rather than a chosen option.

    Make it doable in one pass:
    - where: URL of the exact page to do it on (deep link, not a homepage).
    - steps: list of short steps, in order.
    - values: [{label, value}] the human copies (DNS name/value, a setting) — shown
      with copy buttons.
    - inputs: [{name, label, secret?}] what you need back. Secret inputs (API keys,
      webhook secrets) are stored encrypted locally, never in the repo; fetch one
      with get_action_secret(decision_id, name) after it's answered.
    - done_when: how anyone can tell it's done.
    - verify: a check run when the human clicks Done, one of
      {type: dns, name, record_type, contains?} · {type: http, url, status?, contains?}
      · {type: file, path} (in the repo) · {type: env, name} (set in the repo's .env)
      · {type: pr_merged, url}. No shell commands.
    - est_minutes: your estimate of the human's time.
    """
    from apo import verify as checks

    if verify is not None and verify.get("type") not in checks.CHECK_TYPES:
        return {"error": f"verify.type must be one of {', '.join(checks.CHECK_TYPES)}"}
    spec = {k: v for k, v in {"where": where, "steps": steps, "values": values, "inputs": inputs,
                              "done_when": done_when, "verify": verify}.items() if v}
    conn = db.get_connection()
    proj, repository_id = _resolve_filing(conn, project, repository)
    action_id = db.create_action_request(
        conn,
        project_id=proj["id"],
        repository_id=repository_id,
        title=title,
        question=question,
        summary=summary,
        context=context,
        why_it_matters=why_it_matters,
        blocked_work=blocked_work,
        safe_work=safe_work,
        resume_plan=resume_plan,
        origin_session_id=_origin_session_id(session_id),
        action_spec=spec or None,
        est_minutes=est_minutes,
    )
    return {"id": action_id}


@mcp.tool()
def get_action_secret(decision_id: int, name: str) -> dict:
    """A secret the human provided for one of your Actions (an input marked secret).
    Use it where it's needed — an env file, a deploy secret — never echo it into
    files that get committed, and never into a decision or answer."""
    conn = db.get_connection()
    value = db.get_action_secret(conn, decision_id, name)
    if value is None:
        return {"error": f"No secret {name!r} was provided for #{decision_id} (has it been answered?)."}
    return {"value": value}


@mcp.tool()
def register_repository(project: str, local_path: str, name: str | None = None) -> dict:
    """Register a repository under a project so answers to its decisions get written
    there. Without this, decisions get repository_id=NULL and their answers have
    nowhere to land. name defaults to the project name."""
    conn = db.get_connection()
    proj = db.get_or_create_project(conn, project)
    repo = db.create_repository(conn, proj["id"], name or project, local_path)
    return {"id": repo["id"], "name": repo["name"], "local_path": repo["local_path"]}


@mcp.tool()
def list_open_questions(project: str | None = None) -> list[dict]:
    """List decision requests still waiting on an answer."""
    conn = db.get_connection()
    return [db.get_decision_request(conn, row["id"]) for row in db.list_decision_requests(conn, project, status="open")]


@mcp.tool()
def list_resolved_questions(project: str | None = None) -> list[dict]:
    """List decision requests that have an answer (answered or resolved)."""
    conn = db.get_connection()
    rows = list(db.list_decision_requests(conn, project, status="answered"))
    rows += list(db.list_decision_requests(conn, project, status="resolved"))
    return [db.get_decision_request(conn, row["id"]) for row in rows]


@mcp.tool()
def answer_decision_request(decision_id: int, answer_text: str) -> dict:
    """Record an answer for a decision request — the same path a dashboard or Linear
    answer goes through, for when a decision gets resolved conversationally (in chat)
    instead. Writes the answer into the target repository's `.claude/decisions/` so
    whichever session works that repo next picks it up. Idempotent: if the decision was
    already answered elsewhere, this is a no-op and returns recorded=False."""
    conn = db.get_connection()
    recorded = db.answer_decision_request(conn, decision_id, answer_text, source="mcp")
    return {"recorded": recorded}


@mcp.tool()
def mark_decision_resolved(decision_id: int) -> dict:
    """Mark an answered decision resolved once you've done the work it was blocking.
    This is what stops it showing as ANSWERED in `.claude/decisions/INDEX.md` and in
    the session-start nudge, so call it when finished. No-op (resolved=False) if the
    decision isn't currently in the 'answered' state."""
    conn = db.get_connection()
    resolved = db.resolve_decision_request(conn, decision_id, cwd=os.getcwd())
    return {"resolved": resolved}


@mcp.tool()
def create_outcome(project: str, title: str, done_when: str | None = None, steps: list | None = None) -> dict:
    """Start an outcome: a result the human wants ("Launch the site"), with steps that
    may be yours, an agent's, or the human's — the shared checklist for work that
    spans sessions. Use it for multi-step work that includes a step only the human can
    do or that will outlast this session; not for a one-off fix.

    steps: list of {title, owner, detail?, waiting_on?} where owner is "agent" (an
    agent does it), "me" (the human does it — it shows in their queue), or "external"
    (waiting on someone outside, e.g. DNS propagation, App Store review). Steps are
    ordered as given. This session is linked to the outcome."""
    conn = db.get_connection()
    proj, _ = _resolve_filing(conn, project, None)
    session = _origin_session_id(None)
    outcome_id = db.create_outcome(conn, proj["id"], title, done_when=done_when, created_by="agent", session_id=session)
    step_ids = []
    for step in steps or []:
        step_ids.append(db.add_step(
            conn, outcome_id, step["title"], owner=step.get("owner", "agent"), detail=step.get("detail"),
            waiting_on=step.get("waiting_on"), requested_by="agent", origin_session_id=session,
        ))
    return {"id": outcome_id, "step_ids": step_ids}


@mcp.tool()
def add_step(
    outcome_id: int, title: str, owner: str, detail: str | None = None, waiting_on: str | None = None,
    blocked_by: list | None = None,
) -> dict:
    """Add a step to an outcome. owner: "agent", "me" (the human), or "external".
    blocked_by: ids of steps that must be done first."""
    conn = db.get_connection()
    outcome = db.get_outcome(conn, outcome_id)
    here = db.find_repository_by_path(conn, os.getcwd())
    try:
        step_id = db.add_step(
            conn, outcome_id, title, owner=owner, detail=detail, waiting_on=waiting_on,
            blocked_by=[int(b) for b in blocked_by or []], requested_by="agent",
            origin_session_id=_origin_session_id(None),
            repository_id=here["id"] if here and outcome and here["project_id"] == outcome["project_id"] else None,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"id": step_id}


def _project_for(conn, project: str | None):
    """The named project, or the one this folder belongs to."""
    if project:
        return db.find_project(conn, project)
    repo = db.find_repository_by_path(conn, os.getcwd())
    return conn.execute("SELECT * FROM projects WHERE id = ?", (repo["project_id"],)).fetchone() if repo else None


@mcp.tool()
def get_project_brief(project: str | None = None) -> dict:
    """The project's brief (default: the one this folder belongs to): its purpose,
    how the human will know it's done, its links, and its status. Read it before
    planning work."""
    conn = db.get_connection()
    proj = _project_for(conn, project)
    if proj is None:
        return {"error": f"No project {'called ' + project if project else 'for this folder'}."}
    brief = db.get_project_brief(conn, proj["id"])
    status = db.project_status(conn, proj["id"])
    return {"project": proj["name"], "purpose": brief["goal"], "done_when": brief["done_when"], "links": brief["links"],
            "status": status["value"], "status_reason": status["note"] if status["overridden"] else status["reason"],
            "priority": db.project_priority(conn, proj["id"]),
            "standing_rules": [r["text"] for r in db.list_standing_rules(conn, proj["id"])]}


@mcp.tool()
def list_outcomes(project: str | None = None) -> list[dict]:
    """The project's goals (default: the one this folder belongs to) with their steps:
    who owns each, what's done, what's blocked. See where your work fits before adding
    to them."""
    conn = db.get_connection()
    proj = _project_for(conn, project)
    if proj is None:
        return []
    goals = []
    for outcome in db.list_outcomes(conn, project_id=proj["id"]):
        progress = outcome["progress"]
        goals.append({
            "id": outcome["id"], "title": outcome["title"], "status": outcome["status"], "done_when": outcome["done_when"],
            "due_on": outcome.get("due_on"),
            "description": outcome["description"], "linear_url": outcome["linear_url"],
            "progress": f"{progress['done']}/{progress['total']}",
            "steps": [{"id": s["id"], "title": s["title"], "owner": s["owner"], "status": s["status"], "done": s["done"],
                       "blocked_by": s["blocked_by"]} for s in progress["steps"]],
        })
    return goals


@mcp.tool()
def list_tasks(project: str | None = None) -> list[dict]:
    """Tasks assigned to agents in this project (default: the one this folder belongs
    to) that aren't done: ready ones, then ones another session has claimed. Work one
    only when the user asks you to: claim_task, do it, complete_task."""
    conn = db.get_connection()
    proj = _project_for(conn, project)
    if proj is None:
        return []
    stale = {t["id"] for t in db.stuck_claims(conn, proj["id"])}
    return [
        {"id": t["id"], "title": t["title"], "detail": t["question"], "status": t["status"],
         "claimed_by_session_id": t["claimed_by_session_id"], "outcome_id": t["outcome_id"],
         "blocked_by": t["blocked_by"], "claim_stale": t["id"] in stale}
        for t in db.list_tasks(conn, proj["id"])
    ]


@mcp.tool()
def set_task_blockers(task_id: int, add: list | None = None, remove: list | None = None) -> dict:
    """Change which items must be done before this one. add/remove: ids. A task with an
    unfinished blocker shows as WAITING rather than READY; links that would make a cycle
    are refused."""
    conn = db.get_connection()
    try:
        for blocker in add or []:
            db.link_blocker(conn, int(task_id), int(blocker), by="agent")
        for blocker in remove or []:
            db.remove_dependency(conn, int(task_id), int(blocker), by="agent")
    except ValueError as exc:
        return {"error": str(exc)}
    return {"id": int(task_id), "blocked_by": db.open_blockers(conn, int(task_id))}


@mcp.tool()
def claim_task(task_id: int) -> dict:
    """Take a ready task before starting it. claimed=False means another session has
    it (or it isn't a ready task) — leave it."""
    return {"claimed": db.claim_task(db.get_connection(), task_id, _origin_session_id(None) or "unknown")}


@mcp.tool()
def complete_task(
    task_id: int, note: str | None = None, outcome: str | None = None, done: str | None = None,
    not_done: str | None = None, left_for_human: str | None = None, branch: str | None = None,
) -> dict:
    """Mark a task you finished done; note says what you did. Optional: outcome 'done'
    (default) or 'partial' when part is left, which stays flagged as partly done;
    done / not_done say what was and wasn't; left_for_human is what only the user can
    do (merge, deploy); branch is the local branch the work is committed on, exactly
    as git names it, which the item's page offers to merge with one click."""
    try:
        completed = db.complete_task(db.get_connection(), task_id, note=note, cwd=os.getcwd(), outcome=outcome,
                                     done=done, not_done=not_done, left_for_human=left_for_human, branch=branch)
    except ValueError as exc:
        return {"completed": False, "error": str(exc)}
    return {"completed": completed}


@mcp.tool()
def retry_task(task_id: int, note: str | None = None) -> dict:
    """Put a failed, abandoned, cancelled or stuck-claimed task back to ready, releasing
    its claim. History is kept. Only when the user asks you to."""
    return {"retried": db.retry_task(db.get_connection(), task_id, note=note, by="agent")}


@mcp.tool()
def add_note(item_id: int, body: str) -> dict:
    """Leave a note on an item (a decision, task or action): progress, a finding, a
    question for whoever picks it up next. The user sees it on the item's page, and it
    is in the item's file under .claude/decisions/ for the next session."""
    author = _origin_session_id(None) or "agent"
    try:
        return {"added": True, "note_id": db.add_note(db.get_connection(), item_id, body, author=author)}
    except ValueError as exc:
        return {"added": False, "error": str(exc)}


@mcp.tool()
def withdraw_request(request_id: int, reason: str) -> dict:
    """Withdraw a decision or action you filed that's no longer needed (you found the
    answer, the work changed). It leaves the human's queue."""
    return {"withdrawn": db.cancel_request(db.get_connection(), request_id, reason)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
