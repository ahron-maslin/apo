"""Projects a repository's decisions into markdown inside the repository itself.

The DB stays the source of truth — every answer channel (dashboard, Linear, MCP,
CLI, and Slack when it lands) funnels through db.py, and db.py calls in here on each
state change. These files are a *generated projection*: an agent reads them, works
the ANSWERED ones, and closes each out through `cli.py resolve` /
`mark_decision_resolved`, which updates the DB and rewrites these files in turn. A
hand-edit to them is silently overwritten by the next sync, which is why the
generated header says so.

Why markdown in the repo at all, when the DB already has everything: the answer has
to reach a Claude Code session that is *not* the one that asked, possibly days later,
without that session paying for a query. A file in the repo is greppable, survives a
clone, versions alongside the work it caused, and lets `hooks/session-decisions-nudge.sh`
answer "is there anything new?" in pure grep — no python, no venv, no running server.

INDEX.md's layout is load-bearing for that: ANSWERED rows sort above OPEN above DONE,
so reading the head of the file is enough to see everything actionable no matter how
much resolved history has accumulated below it.
"""
import os
import re
import shutil
import sqlite3
from pathlib import Path

DECISIONS_SUBDIR = Path(".claude") / "decisions"
INDEX_FILENAME = "INDEX.md"

_STATUS_LABELS = {
    "open": "OPEN", "answered": "ANSWERED", "resolved": "DONE", "in_progress": "IN PROGRESS", "cancelled": "CANCELLED",
}
_STATUS_ORDER = {"ANSWERED": 0, "READY": 1, "IN PROGRESS": 2, "OPEN": 3, "WAITING": 4, "DONE": 5, "CANCELLED": 6}
_KIND_TITLES = {"decision": "Decision", "action": "Action", "task": "Task", "review": "Review", "wait": "Wait"}
_DECISION_FILE_RE = re.compile(r"^\d{4}-.*\.md$")


def decisions_dir(local_path: str) -> Path:
    return Path(local_path) / DECISIONS_SUBDIR


def sync_decision(conn: sqlite3.Connection, decision_id: int) -> Path | None:
    """Rewrite the projection for whichever repository owns this decision. Only this
    decision's own file is re-rendered — every other decision in the project keeps
    its existing file untouched (see sync_repository). Returns the index path, or
    None when the decision has no repository to write into."""
    decision = conn.execute(
        "SELECT project_id, repository_id FROM decision_requests WHERE id = ?", (decision_id,)
    ).fetchone()
    if not decision:
        return None
    repository = _resolve_repository(conn, decision["repository_id"], decision["project_id"])
    return sync_repository(conn, repository, changed_decision_ids={decision_id}) if repository else None


def sync_repository(
    conn: sqlite3.Connection, repository: dict, changed_decision_ids: set[int] | None = None
) -> Path:
    """Rewrite INDEX.md plus whichever decision files actually changed for one
    repository, and drop decision files that no longer correspond to a live row.

    A decision's rendered content never changes once written except through its own
    state transition, so only `changed_decision_ids` gets a full re-render — every
    other decision here is a cheap existence check, not a query + JSON-decode + write.
    A file that's missing outright (a repository registered after decisions already
    existed, or a hand-deleted file) is treated as changed too, so the projection
    self-heals without paying for everything else in the project.
    """
    from apo import db

    changed_decision_ids = changed_decision_ids or set()

    # The projection is per folder, not per project: if one folder is registered under
    # more than one project (a rename, before `ado project-merge`), every decision
    # filed against it belongs here. Projecting one project alone would prune the
    # other's files as "stale".
    owner_cache: dict[tuple, dict | None] = {}

    def owner_of(repository_id, project_id):
        key = (repository_id, project_id)
        if key not in owner_cache:
            owner_cache[key] = _resolve_repository(conn, repository_id, project_id)
        return owner_cache[key]

    mine = []
    project_names: dict[int, str] = {}
    for repo in _repositories_at(conn, repository):
        project_names[repo["project_id"]] = conn.execute(
            "SELECT name FROM projects WHERE id = ?", (repo["project_id"],)
        ).fetchone()["name"]
        for summary in db.list_decision_summaries(conn, repo["project_id"]):
            owner = owner_of(summary["repository_id"], repo["project_id"])
            if owner and owner["id"] == repo["id"]:
                mine.append((dict(summary), project_names[repo["project_id"]]))
    mine.sort(key=lambda pair: pair[0]["id"])
    for summary, _ in mine:
        if summary["status"] == "open" and summary["kind"] == "task":
            summary["blocked_by"] = db.open_blockers(conn, summary["id"])
    project_name = ", ".join(sorted(set(project_names.values())))

    target = decisions_dir(repository["local_path"])
    target.mkdir(parents=True, exist_ok=True)

    written = set()
    for summary, owner_project_name in mine:
        filename = _decision_filename(summary)
        written.add(filename)
        path = target / filename
        if summary["id"] in changed_decision_ids or not path.exists():
            full = db.get_decision_request(conn, summary["id"])
            full["attachments"] = _copy_attachments(conn, summary["id"], repository["local_path"])
            full["notes"] = db.list_notes(conn, summary["id"])
            path.write_text(_render_decision(full, owner_project_name), encoding="utf-8")

    # Only ever considers NNNN-*.md, so anything else a human put in this directory
    # (notes, a README) survives.
    for existing in target.iterdir():
        if existing.name not in written and _DECISION_FILE_RE.match(existing.name):
            existing.unlink()

    index_path = target / INDEX_FILENAME
    index_path.write_text(_render_index([s for s, _ in mine], project_name), encoding="utf-8")
    return index_path


def _repositories_at(conn: sqlite3.Connection, repository: dict) -> list[dict]:
    """Every registered repository at the same folder as `repository`, itself first."""
    folder = repository["local_path"].rstrip("/")
    siblings = [
        dict(row) for row in conn.execute("SELECT * FROM repositories WHERE id != ?", (repository["id"],))
        if row["local_path"].rstrip("/") == folder
    ]
    return [dict(repository), *siblings]


def _copy_attachments(conn: sqlite3.Connection, decision_id: int, local_path: str) -> list[dict]:
    """Copy each attachment into the repo alongside the markdown that links to it, so
    the resuming agent's Read tool can actually see the screenshot. Non-fatal per
    attachment, same reasoning as the rest of this module: a missing source file, an
    unwritable target, or a moved/deleted repo must not take down the markdown write
    for everything else — record a sync_errors row and just skip that one image's
    link rather than rendering a dangling reference."""
    from apo import db

    attachments = db.list_attachments(conn, decision_id)
    if not attachments:
        return []

    from apo import config

    target_dir = Path(local_path) / DECISIONS_SUBDIR / "attachments" / f"{decision_id:04d}"
    copied = []
    for attachment in attachments:
        source = config.get_attachments_dir() / str(decision_id) / attachment["stored_name"]
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target_dir / attachment["stored_name"])
        except OSError as exc:
            db.record_sync_error(conn, "repo_writer", decision_id, str(exc))
            continue
        copied.append(attachment)
    return copied


def _resolve_repository(conn: sqlite3.Connection, repository_id: int | None, project_id: int):
    from apo import db

    if repository_id:
        return db.get_repository(conn, repository_id)
    return db.get_repository_for_project(conn, project_id)


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:48].rstrip("-") or "decision"


def _decision_filename(decision: dict) -> str:
    return f"{decision['id']:04d}-{_slugify(decision['title'])}.md"


def _label(decision: dict) -> str:
    if decision["status"] == "open" and decision["kind"] == "task":
        # Assigned to an agent and not claimed yet; WAITING while a step it depends on is open.
        return "WAITING" if decision.get("blocked_by") else "READY"
    if decision["status"] == "open" and decision["kind"] == "wait":
        return "WAITING"  # on someone outside — neither the human nor an agent
    return _STATUS_LABELS.get(decision["status"], decision["status"].upper())


def _render_index(decisions: list[dict], project_name: str) -> str:
    counts = {"ANSWERED": 0, "READY": 0, "OPEN": 0, "DONE": 0}
    for decision in decisions:
        counts[_label(decision)] = counts.get(_label(decision), 0) + 1

    ordered = sorted(decisions, key=lambda d: (_STATUS_ORDER.get(_label(d), 9), d.get("work_rank") is None,
                                                 d.get("work_rank") or 0, d["id"]))
    lines = [
        f"# Decisions — {project_name}",
        "",
        f"<!-- ADO answered={counts['ANSWERED']} open={counts['OPEN']} done={counts['DONE']} ready={counts['READY']} -->",
        "",
        "ANSWERED rows have a human answer waiting and need work — run `/decisions` to pick",
        "them up. READY rows are tasks the human assigned to an agent, picked up the same way.",
        "OPEN rows are still waiting on a human: do not act on them, and do not re-ask them.",
        "WAITING rows wait on someone outside, or on another item finishing first. DONE and",
        "CANCELLED rows are finished history.",
        "",
        "Generated by ai-decision-orchestrator — edits here are overwritten. Close a decision",
        "out with `mark_decision_resolved` (MCP) or `cli.py resolve <id>`.",
        "",
        "| status | id | title | file |",
        "| --- | --- | --- | --- |",
    ]
    for decision in ordered:
        title = _escape_cell(decision["title"])
        if decision["kind"] in ("action", "task", "wait", "review"):
            title = f"[{decision['kind'].upper()}] {title}"
        lines.append(
            f"| {_label(decision)} | {decision['id']:04d} | {title} "
            f"| {_decision_filename(decision)} |"
        )
    if not ordered:
        lines.append("| | | _no decisions filed_ | |")
    return "\n".join(lines) + "\n"


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _render_decision(decision: dict, project_name: str) -> str:
    from apo import db

    label = _label(decision)
    is_action = decision["kind"] == "action"
    lines = [
        f"# {decision['id']:04d} — {decision['title']}",
        "",
        f"- **Status:** {label}",
        f"- **Kind:** {_KIND_TITLES.get(decision['kind'], 'Decision')}",
        f"- **Project:** {project_name}",
        f"- **Filed:** {decision['created_at']}",
    ]
    if decision["answered_at"]:
        via = f" (via {decision['answer_source']})" if decision["answer_source"] else ""
        lines.append(f"- **Answered:** {decision['answered_at']}{via}")
    if decision["resolved_at"]:
        lines.append(f"- **Closed:** {decision['resolved_at']}")
    where = db.work_location_phrase(decision.get("work_location"))
    if where:
        # This file isn't rewritten when the branch is merged later.
        lines.append(f"- **Where the work is:** {where}{' when it closed' if 'not merged' in where else ''}")
    if decision.get("linear_url"):
        lines.append(f"- **Linear:** {decision['linear_url']}")

    lines += ["", "## Question", "", decision["question"]]

    if decision["answer_text"]:
        lines += ["", "## Answer", "", decision["answer_text"]]

    completion = decision.get("completion")
    if completion:
        lines += ["", "## Completion", ""]
        for key, label in (("outcome", "Outcome"), ("done", "Done"), ("not_done", "Not done"),
                           ("left_for_human", "Left for you"), ("branch", "Branch")):
            if completion.get(key):
                lines.append(f"- **{label}:** {completion[key]}")

    if decision.get("attachments"):
        lines += ["", "## Screenshots", ""]
        for attachment in decision["attachments"]:
            # Escape ']'/'[' in the alt text only — it's the one part of the line
            # built from a user-supplied string (the original upload filename); the
            # path half is always a server-generated uuid, never attacker-influenced.
            alt = attachment["filename"].replace("[", "\\[").replace("]", "\\]")
            lines.append(f"![{alt}](attachments/{decision['id']:04d}/{attachment['stored_name']})")

    for heading, value in (("Context", decision["context"]), ("Why it matters", decision["why_it_matters"])):
        if value:
            lines += ["", f"## {heading}", "", value]

    if decision["options"]:
        lines += ["", "## Options", ""]
        for option in decision["options"]:
            if isinstance(option, dict):
                detail = f" — {option['detail']}" if option.get("detail") else ""
                lines.append(f"- {option.get('label', '')}{detail}")
            else:
                lines.append(f"- {option}")

    if decision["recommendation"]:
        confidence = f" (confidence {decision['confidence']}%)" if decision["confidence"] else ""
        lines += ["", f"## Agent recommendation{confidence}", "", decision["recommendation"]]

    for heading, items in (("Blocked work", decision["blocked_work"]), ("Safe to continue", decision["safe_work"])):
        if items:
            lines += ["", f"## {heading}", ""] + [f"- {item}" for item in items]

    plan = decision.get("resume_plan")
    if plan:
        lines += ["", "## Resume plan", ""]
        if plan.get("objective"):
            lines.append(f"**Objective:** {plan['objective']}")
        if plan.get("files"):
            lines += ["", "**Files:**"] + [f"- {f}" for f in plan["files"]]
        if plan.get("previous_reasoning"):
            lines += ["", f"**Where it left off:** {plan['previous_reasoning']}"]
        if plan.get("remaining_steps"):
            lines += ["", "**Remaining steps:**"] + [f"1. {s}" for s in plan["remaining_steps"]]
        if plan.get("expected_outcome"):
            lines += ["", f"**Expected outcome:** {plan['expected_outcome']}"]

    if label == "ANSWERED":
        instruction = (
            "Confirm the action above is actually done" if is_action else "Do the work above"
        )
        lines += [
            "",
            "---",
            "",
            f"{instruction}, then close this out so it stops showing as actionable:",
            f"`mark_decision_resolved(decision_id={decision['id']})`, or "
            f"`python3 {_cli_path()} resolve {decision['id']}`.",
        ]
    elif label == "READY":
        lines += [
            "",
            "---",
            "",
            "This task is assigned to an agent. When the user asks you to work it (e.g. via",
            f"`/decisions`): `claim_task(task_id={decision['id']})`, do the work, then",
            f"`complete_task(task_id={decision['id']}, note=...)` saying what you did.",
        ]
    if decision.get("cancel_reason"):
        lines += ["", f"**Cancelled:** {decision['cancel_reason']}"]
    if decision.get("notes"):
        # Last on purpose: nothing above moves when the first note arrives.
        lines += ["", "## Notes", ""]
        for note in decision["notes"]:
            body = note["body"].replace("\n", "\n  ")
            lines.append(f"- **{note['author']}** ({note['created_at']}): {body}")
    return "\n".join(lines) + "\n"


def _cli_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.py")


_POLICY_MARK = "<!-- Written by Control Center"  # the old name; files already written start with it


def write_policy(local_path: str, project_name: str, rules: list[dict]) -> None:
    """.claude/decisions/POLICY.md: the project's standing rules, for agents to read before
    filing. Removed when there are none, but only if Switchboard wrote it."""
    path = decisions_dir(local_path) / "POLICY.md"
    if not rules:
        if path.exists() and path.read_text().startswith(_POLICY_MARK):
            path.unlink()
        return
    lines = [f"{_POLICY_MARK}; edit the rules on the project's Overview. -->",
             f"# Standing rules for {project_name}", "",
             "Read these before filing a decision. If a rule answers the question, follow it instead of "
             "filing, and say in your summary which rule you followed.", ""]
    for n, rule in enumerate(rules, 1):
        source = f" (from #{rule['source_decision_id']})" if rule.get("source_decision_id") else ""
        lines.append(f"{n}. {rule['text']}{source}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
