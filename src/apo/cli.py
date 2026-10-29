"""apo: a local dashboard and MCP server for Claude Code.

Set it up with `apo init`. The rest is the dashboard, the MCP server, the hook targets
Claude Code calls, and a command line for agents and scripts that can't use MCP.
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import apo
from apo import db

# When this hook process started, which is within milliseconds of the event that
# spawned it. Async hooks can finish out of order, so mid-turn writes are guarded
# against landing after the turn's Stop (see db._turn_guard).
_EVENT_AT = datetime.now(timezone.utc).isoformat()


def _cmd_ask(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    project, repository_id = db.resolve_project_for_filing(conn, args.project, os.getcwd())
    decision_id = db.create_decision_request(
        conn,
        project_id=project["id"],
        repository_id=repository_id,
        title=args.title,
        question=args.question,
        summary=args.summary,
        context=args.context,
        why_it_matters=args.why_it_matters,
        recommendation=args.recommendation,
        confidence=args.confidence,
        blocked_work=json.loads(args.blocked_work) if args.blocked_work else None,
        safe_work=json.loads(args.safe_work) if args.safe_work else None,
        origin_session_id=args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID"),
    )
    print(f"Filed decision request #{decision_id} for project '{args.project}'.")


def _cmd_act(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    project, repository_id = db.resolve_project_for_filing(conn, args.project, os.getcwd())
    action_id = db.create_action_request(
        conn,
        project_id=project["id"],
        repository_id=repository_id,
        title=args.title,
        question=args.question,
        summary=args.summary,
        context=args.context,
        why_it_matters=args.why_it_matters,
        blocked_work=json.loads(args.blocked_work) if args.blocked_work else None,
        safe_work=json.loads(args.safe_work) if args.safe_work else None,
        origin_session_id=args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID"),
    )
    print(f"Filed action request #{action_id} for project '{args.project}'.")


def _cmd_bug(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    if args.project:
        project, repository_id = db.resolve_project_for_filing(conn, args.project, os.getcwd())
    else:
        repo = db.find_repository_by_path(conn, os.getcwd())
        if repo is None:
            print(f"{os.getcwd()} isn't a project's folder — run this from one, or pass --project NAME.",
                  file=sys.stderr)
            sys.exit(1)
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (repo["project_id"],)).fetchone()
        repository_id = repo["id"]
    bug_id = db.report_bug(
        conn, project["id"], args.title, happened=args.happened, expected=args.expected, steps=args.steps,
        severity=args.severity, repository_id=repository_id,
    )
    print(f"Filed bug #{bug_id} for project '{project['name']}'.")


def _cmd_status(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    project_id = None
    if args.project:
        project = db.find_project(conn, args.project)
        if project is None:
            print(f"No project named '{args.project}'.", file=sys.stderr)
            sys.exit(1)
        project_id = project["id"]
    try:
        report = db.status_report(conn, project_id, db.parse_since(args.since))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    print(json.dumps(report, indent=2) if args.format == "json" else db.status_markdown(report), end="\n" if args.format == "json" else "")


def _cmd_answer(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    recorded = db.answer_decision_request(conn, args.decision_id, args.answer_text, source="cli")
    if not recorded:
        print(f"Decision request #{args.decision_id} was already answered — nothing done.")
        return
    print(f"Recorded answer for #{args.decision_id}.")
    decision = db.get_decision_request(conn, args.decision_id)
    repository = (
        db.get_repository(conn, decision["repository_id"])
        if decision["repository_id"]
        else db.get_repository_for_project(conn, decision["project_id"])
    )
    if repository:
        from apo import repo_writer
        print(f"Written to {repo_writer.decisions_dir(repository['local_path'])} — it'll be "
              "picked up next time a session starts there.")
    else:
        print("No repository registered for this project, so nothing was written to disk. "
              "Register one with `repo-add` if you want the answer to reach a repo.")


def _cmd_repo_add(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    project = db.get_or_create_project(conn, args.project)
    repo = db.create_repository(conn, project["id"], args.name or args.project, args.path)
    print(f"Registered repository '{repo['name']}' -> {repo['local_path']} for project '{args.project}'.")


def _cmd_list(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    rows = db.list_decision_requests(conn, args.project, args.status, args.search)
    if args.json:
        print(json.dumps([
            {
                "id": row["id"], "project_name": row["project_name"], "status": row["status"],
                "title": row["title"], "question": row["question"], "answer_text": row["answer_text"],
            }
            for row in rows
        ]))
        return
    if not rows:
        print("No matching decision requests.")
        return
    for row in rows:
        print(f"[#{row['id']}] {row['project_name']} — {row['status'].upper()}")
        print(f"    {row['title']}")
        print(f"    Q: {row['question']}")
        if row["answer_text"]:
            print(f"    A: {row['answer_text']}")
        print()


def _cmd_show(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    decision = db.get_decision_request(conn, args.decision_id)
    if not decision:
        print(f"No decision request #{args.decision_id}.")
        return

    if args.json:
        print(json.dumps(decision))
        return

    print(f"[#{decision['id']:04d}] {decision['title']} — {decision['status'].upper()}")
    if decision.get("summary"):
        print(f"  {decision['summary']}")
    print(f"  Q: {decision['question']}")

    if decision.get("context"):
        print(f"\nContext:\n  {decision['context']}")
    if decision.get("why_it_matters"):
        print(f"\nWhy it matters:\n  {decision['why_it_matters']}")
    if decision.get("options"):
        print("\nOptions:")
        for option in decision["options"]:
            detail = f" — {option['detail']}" if option.get("detail") else ""
            print(f"  - {option.get('label', '')}{detail}")
    if decision.get("recommendation"):
        confidence = f" (confidence {decision['confidence']}%)" if decision.get("confidence") else ""
        print(f"\nRecommendation{confidence}:\n  {decision['recommendation']}")
    if decision.get("blocked_work"):
        print("\nBlocked work:")
        for item in decision["blocked_work"]:
            print(f"  - {item}")
    if decision.get("safe_work"):
        print("\nSafe to continue:")
        for item in decision["safe_work"]:
            print(f"  - {item}")

    if decision.get("answer_text"):
        via = f" (via {decision['answer_source']})" if decision.get("answer_source") else ""
        print(f"\nAnswer{via}:\n  {decision['answer_text']}")

    plan = decision.get("resume_plan")
    if plan:
        print("\nResume plan:")
        if plan.get("objective"):
            print(f"  Objective: {plan['objective']}")
        if plan.get("files"):
            print("  Files:")
            for f in plan["files"]:
                print(f"    - {f}")
        if plan.get("previous_reasoning"):
            print(f"  Where it left off: {plan['previous_reasoning']}")
        if plan.get("remaining_steps"):
            print("  Remaining steps:")
            for i, step in enumerate(plan["remaining_steps"], 1):
                print(f"    {i}. {step}")
        if plan.get("expected_outcome"):
            print(f"  Expected outcome: {plan['expected_outcome']}")

    if decision.get("linear_url"):
        print(f"\nLinear: {decision['linear_url']}")


def _cmd_pending_all(conn, as_json: bool) -> None:
    """Every project's work waiting for an agent: answers not yet applied and READY
    tasks, with the folder to work them in — for a session started somewhere else."""
    items = db.waiting_for_agents(conn)
    if as_json:
        print(json.dumps(items))
        return
    if not items:
        print("Nothing is waiting for an agent in any project.")
        return
    by_path: dict = {}
    for item in items:
        by_path.setdefault((item["project"], item["path"]), []).append(item)
    for (project, path), group in by_path.items():
        folders = group[0].get("folders")
        print(f"{project} — {path or ('no folder chosen' if folders else 'no folder registered')}")
        for item in group:
            print(f"  #{item['id']} {item['title']} ({item['state']})")
        if path:
            print(f"  Work them: {group[0]['command']}")
        elif folders:
            print("  Agents can't see these until each is in one of the project's folders:")
            for folder in folders:
                print(f"    {folder}")
            for item in group:
                print(f"  apo move {item['id']} <folder>")
        print()


def _cmd_tasks(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    path = args.path or os.getcwd()
    repo = db.find_repository_by_path(conn, path)
    tasks = db.list_tasks(conn, repo["project_id"]) if repo else []
    if args.json:
        print(json.dumps([{"id": t["id"], "title": t["title"], "status": t["status"], "detail": t["question"],
                           "blocked_by": t["blocked_by"]} for t in tasks]))
        return
    if not tasks:
        print(f"No agent tasks for {path}.")
        return
    for t in tasks:
        state = "IN PROGRESS" if t["status"] == "in_progress" else ("WAITING" if t["blocked_by"] else "READY")
        print(f"[#{t['id']}] {state} {t['title']}")


def _cmd_block(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    try:
        for blocker in args.on:
            db.link_blocker(conn, args.task_id, blocker)
        for blocker in args.remove:
            db.remove_dependency(conn, args.task_id, blocker)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    waits = db.open_blockers(conn, args.task_id)
    print(f"#{args.task_id} " + ("waits on " + ", ".join(f"#{b}" for b in waits) if waits else "is not waiting on anything"))


def _cmd_claim(args: argparse.Namespace) -> None:
    """The CLI twin of the claim_task MCP tool — for a session whose tool list
    predates it (a resumed session keeps the tools it was created with)."""
    session = os.environ.get("CLAUDE_CODE_SESSION_ID") or "cli"
    if not db.claim_task(db.get_connection(), args.task_id, session):
        print(f"#{args.task_id} isn't a ready task (another session may have it) — leave it.")
        sys.exit(1)
    print(f"Claimed #{args.task_id}.")


def _cmd_complete(args: argparse.Namespace) -> None:
    try:
        completed = db.complete_task(
            db.get_connection(), args.task_id, note=args.note, cwd=os.getcwd(), outcome=args.outcome,
            done=args.done, not_done=args.not_done, left_for_human=args.left_for_human, branch=args.branch)
    except ValueError as exc:
        print(exc)
        sys.exit(1)
    if not completed:
        print(f"#{args.task_id} isn't an open task.")
        sys.exit(1)
    print(f"Completed #{args.task_id}.")


def _cmd_note(args: argparse.Namespace) -> None:
    author = os.environ.get("CLAUDE_CODE_SESSION_ID") or "human"
    try:
        db.add_note(db.get_connection(), args.item_id, args.text, author=author)
    except ValueError as exc:
        print(exc)
        sys.exit(1)
    print(f"Noted on #{args.item_id}.")


def _cmd_retry(args: argparse.Namespace) -> None:
    if not db.retry_task(db.get_connection(), args.task_id, note=args.note):
        print(f"#{args.task_id} isn't a claimed, finished or cancelled task, so there is nothing to retry.")
        sys.exit(1)
    print(f"#{args.task_id} is READY again.")


def _cmd_stuck(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    repo = db.find_repository_by_path(conn, args.path or os.getcwd())
    stuck = db.stuck_claims(conn, repo["project_id"] if repo else None)
    if not stuck:
        print("No claimed task has lost its session.")
        return
    for t in stuck:
        print(f"[#{t['id']}] claimed {t['claimed_at']} by ended session {t['claimed_by_session_id']}: {t['title']}"
              f"  (apo retry {t['id']})")


def _cmd_move(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    item = db.get_decision_request(conn, args.item_id)
    if item is None:
        print(f"No item #{args.item_id}.")
        sys.exit(1)
    folders = db.project_repositories(conn, item["project_id"])
    named = [f for f in folders if f["name"] == args.folder] or [
        f for f in folders if f["name"].lower() == args.folder.lower()]
    target = named[0] if len(named) == 1 else db.find_repository_by_path(conn, os.path.abspath(args.folder))
    if target is None or target["project_id"] != item["project_id"]:
        print(f"{args.folder!r} isn't one of {db.get_project_name(conn, item['project_id'])}'s folders:")
        for f in folders:
            print(f"  {f['name']} — {f['local_path']}")
        sys.exit(1)
    db.move_item(conn, args.item_id, target["id"], by="agent" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "human")
    print(f"Moved #{args.item_id} to {target['local_path']}.")


def _cmd_pending(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    if args.all:
        _cmd_pending_all(conn, args.json)
        return
    path = args.path or os.getcwd()
    repo = db.find_repository_by_path(conn, path)
    rows = []
    if repo:
        rows = [
            db.get_decision_request(conn, row["id"])
            for row in db.list_decision_requests(conn, status="answered")
            if row["repository_id"] == repo["id"]
        ]

    if args.json:
        print(json.dumps([
            {"id": r["id"], "title": r["title"], "question": r["question"], "answer_text": r["answer_text"]}
            for r in rows
        ]))
        return

    if not rows:
        print(f"No answered decisions waiting for {path}.")
        return
    for row in rows:
        print(f"[#{row['id']}] {row['title']}")
        print(f"    Q: {row['question']}")
        print(f"    A: {row['answer_text']}")
        plan = row.get("resume_plan")
        if plan and plan.get("objective"):
            print(f"    Objective: {plan['objective']}")
        if plan and plan.get("remaining_steps"):
            for step in plan["remaining_steps"]:
                print(f"      - {step}")
        print()


def _cmd_resolve(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    resolved = db.resolve_decision_request(conn, args.decision_id, cwd=os.getcwd())
    if resolved:
        print(f"Marked decision request #{args.decision_id} resolved.")
    else:
        print(f"Decision request #{args.decision_id} was not in an answered state — nothing done.")


def _cmd_archive_project(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    project = db.get_or_create_project(conn, args.project)
    result = db.archive_project(conn, project["id"], close_note=args.note)
    if not result["archived"]:
        print(f"Project '{args.project}' is already archived — nothing done.")
        return
    print(f"Archived project '{args.project}'.")
    if result["closed_decision_ids"]:
        ids = ", ".join(f"#{i}" for i in result["closed_decision_ids"])
        print(f"Force-closed {len(result['closed_decision_ids'])} still-open decision(s): {ids}")


def _cmd_projects(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    counts = db.project_counts(conn)
    for project in conn.execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE").fetchall():
        repos = [r["local_path"] for r in conn.execute(
            "SELECT local_path FROM repositories WHERE project_id = ? ORDER BY id", (project["id"],)
        )]
        aliases = [a["alias"] for a in conn.execute(
            "SELECT alias FROM project_aliases WHERE project_id = ? ORDER BY alias", (project["id"],)
        )]
        c = counts.get(project["name"], {"open": 0, "total": 0})
        archived = " [archived]" if project["archived_at"] else ""
        print(f"{project['name']}{archived} — {c['total']} decision(s), {c['open']} open")
        for path in repos:
            print(f"    {path}")
        if aliases:
            print(f"    also known as: {', '.join(aliases)}")
    shared = db.projects_sharing_a_folder(conn)
    if shared:
        print("\nFolders shared by more than one project (a folder should be one project):")
        for entry in shared:
            keep = entry["newest"]
            print(f"  {entry['path']}: {', '.join(entry['projects'])}")
            for retire in (n for n in entry["projects"] if n != keep):
                print(f"    suggested (keeps the newest name): apo project-merge --from \"{retire}\" --into \"{keep}\"")


def _cmd_project_merge(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    source = db.find_project(conn, args.from_project)
    target = db.find_project(conn, args.into_project)
    for name, row in ((args.from_project, source), (args.into_project, target)):
        if row is None:
            print(f"No project named '{name}'.", file=sys.stderr)
            sys.exit(1)
    try:
        result = db.merge_projects(conn, source["id"], target["id"])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(
        f"Merged '{source['name']}' into '{target['name']}': {result['decisions']} decision(s), "
        f"{result['sessions']} session(s), {result['repositories']} repository(ies) moved. "
        f"'{source['name']}' now resolves to '{target['name']}'."
    )


def _cmd_init(args: argparse.Namespace) -> None:
    from apo import installer

    installer.run(args)


def _cmd_mcp(args: argparse.Namespace) -> None:
    from apo import mcp_server

    mcp_server.main()


def _read_hook_payload() -> dict | None:
    """Claude Code hook JSON on stdin. Returns None for anything not shaped like a
    real hook payload (empty, malformed, or missing session_id) — every _cmd_session_*/
    _cmd_checklist_item_* function below treats that as "nothing to do," not an error."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) and data.get("session_id") else None
    except Exception:
        return None


def _run_hook(fn) -> None:
    """Shared fail-open wrapper for every hook-target subcommand: Claude Code execs
    these as real hooks, and a Stop hook in particular can BLOCK the agent's turn if
    it exits nonzero or emits a blocking decision — so this must never raise, never
    print anything but `{}`, and never exit nonzero, regardless of what goes wrong
    (a missing/malformed payload, a DB that can't be reached, anything)."""
    payload = _read_hook_payload()
    output = None
    if payload:
        try:
            output = fn(payload)
        except Exception:
            output = None
    print(json.dumps(output) if isinstance(output, dict) and output else "{}")


def _dashboard_pid_file() -> Path:
    from apo import config

    return config.get_db_path().parent / "dashboard.pid"


def _dashboard_running(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


def _start_dashboard(port: int, dev: bool = False) -> str:
    """Detached from whoever started it. `dev` adds --reload, for working on apo
    itself."""
    from apo import config

    if _dashboard_running(port):
        return "running"
    log = config.get_db_path().parent / "dashboard.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        cmd = [sys.executable, "-m", "uvicorn", "apo.web.app:app", "--host", "127.0.0.1", "--port", str(port)]
        proc = subprocess.Popen(
            cmd + (["--reload"] if dev else []),
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )
    _dashboard_pid_file().write_text(f"{proc.pid}\n")
    return "started"


def _stop_dashboard() -> str:
    pid_file = _dashboard_pid_file()
    if not pid_file.exists():
        return "not started here"
    try:
        # With --dev there's a reloader and a worker; the group takes both.
        os.killpg(int(pid_file.read_text().strip()), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, ValueError):
        pass
    pid_file.unlink(missing_ok=True)
    return "stopped"


def _cmd_dashboard(args: argparse.Namespace) -> None:
    from apo import config

    port = args.port or config.dashboard_port()
    url = f"http://127.0.0.1:{port}"
    if args.action == "run":
        from apo.web import app

        app.run_dashboard(port, reload=args.dev)
    elif args.action == "start":
        print({"started": f"Started the dashboard at {url}",
               "running": f"Already running at {url}"}[_start_dashboard(port, dev=args.dev)])
    elif args.action == "stop":
        print({"stopped": "Stopped the dashboard.",
               "not started here": "No dashboard started by `apo dashboard start` to stop."}[_stop_dashboard()])
    else:
        print(f"Running at {url}" if _dashboard_running(port) else f"Not running (nothing answers at {url})")


def _session_start(p: dict) -> dict | None:
    from apo import config

    if config.dashboard_autostart():
        try:
            _start_dashboard(config.dashboard_port())
        except OSError:
            pass
    conn = db.get_connection()
    db.register_session(conn, p["session_id"], p.get("cwd"), transcript_path=p.get("transcript_path"))
    hint = db.session_folder_hint(conn, p["session_id"], p.get("cwd"))
    if hint:
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": hint}}
    return None


def _cmd_session_start(args: argparse.Namespace) -> None:
    _run_hook(_session_start)


def _sync_checklist_from_tool(conn, p: dict) -> None:
    """The checklist's primary feed. Claude Code's TaskCreated/TaskCompleted hooks
    don't fire for an ordinary session's own TaskCreate/TaskUpdate calls (verified
    against 2.1.291: only PostToolUse did), so the tool calls themselves are mirrored
    here. TodoWrite covers builds that use it instead of the Task tools."""
    tool, tool_input, response = p.get("tool_name"), p.get("tool_input"), p.get("tool_response")
    if not isinstance(tool_input, dict):
        return
    sid = p["session_id"]
    if tool == "TaskCreate":
        task = response.get("task") if isinstance(response, dict) else None
        if isinstance(task, dict) and task.get("id") is not None:
            db.record_checklist_item(
                conn, sid, str(task["id"]), task.get("subject") or tool_input.get("subject") or "(untitled)",
                tool_input.get("description"),
            )
    elif tool == "TaskUpdate":
        if tool_input.get("taskId") is not None:
            db.set_checklist_item_status(
                conn, sid, str(tool_input["taskId"]), tool_input.get("status"), tool_input.get("subject"),
            )
    elif tool == "TodoWrite":
        todos = tool_input.get("todos")
        if isinstance(todos, list):
            db.sync_todo_list(conn, sid, todos)


_CREATE_REQUEST_TOOLS = ("__create_decision_request", "__create_action_request")


def _request_id_from_tool_response(response) -> int | None:
    """An MCP tool result reaches PostToolUse as content blocks — the tool's returned
    dict arrives JSON-encoded in a text block — but tolerate a bare dict or string."""
    if isinstance(response, dict):
        if isinstance(response.get("id"), int):
            return response["id"]
        response = response.get("structuredContent") or response.get("content")
        if isinstance(response, dict):
            return _request_id_from_tool_response(response)
    if isinstance(response, list):
        for block in response:
            if isinstance(block, dict) and block.get("type") == "text":
                found = _request_id_from_tool_response(block.get("text"))
                if found is not None:
                    return found
        return None
    if isinstance(response, str):
        try:
            parsed = json.loads(response)
        except ValueError:
            return None
        return parsed["id"] if isinstance(parsed, dict) and isinstance(parsed.get("id"), int) else None
    return None


def _bind_filed_request(conn, p: dict) -> None:
    """The hook payload for apo's own create_* call is the one place that knows for
    certain which session filed the request (see db.bind_request_to_session)."""
    tool = p.get("tool_name") or ""
    if not (tool.startswith("mcp__") and tool.endswith(_CREATE_REQUEST_TOOLS)):
        return
    request_id = _request_id_from_tool_response(p.get("tool_response"))
    if request_id is not None:
        db.bind_request_to_session(conn, request_id, p["session_id"])


def _heartbeat(p: dict) -> None:
    conn = db.get_connection()
    db.session_heartbeat(conn, p["session_id"], p.get("cwd"), event_at=_EVENT_AT)
    _sync_checklist_from_tool(conn, p)
    _bind_filed_request(conn, p)


PROMPT_POLL_SECONDS = 1.0


def _holds_prompts(conn, session_id: str) -> bool:
    """Background sessions only: a terminal session's own dialog must never wait on
    the dashboard. A session not yet seen by reconcile is looked up first."""
    session = db.get_session(conn, session_id)
    if session is not None and session["agent_kind"] is None:
        from apo import claude_runtime

        db.reconcile_live_sessions(conn, claude_runtime.list_agents())
        session = db.get_session(conn, session_id)
    return session is not None and session["agent_kind"] == "background"


def _hold(conn, session_id: str, prompt: dict) -> dict | None:
    """The dashboard's answer, or None to leave the prompt to Claude Code's own dialog."""
    deadline = time.monotonic() + db.PROMPT_HOLD_SECONDS
    prompt_id = db.hold_prompt(conn, session_id, prompt)
    while prompt_id is None and time.monotonic() < deadline:
        time.sleep(PROMPT_POLL_SECONDS)
        prompt_id = db.hold_prompt(conn, session_id, prompt)
    if prompt_id is None:
        return None
    try:
        while time.monotonic() < deadline:
            answer = db.held_answer(conn, session_id, prompt_id)
            if answer is not None:
                return None if answer.get("behavior") == "terminal" else answer
            time.sleep(PROMPT_POLL_SECONDS)
        return None
    finally:
        db.end_prompt(conn, session_id, prompt_id)


def _permission(p: dict) -> dict | None:
    from apo import transcripts

    conn = db.get_connection()
    detail = transcripts.describe_tool_call(p.get("tool_name"), p.get("tool_input"))
    db.session_waiting(conn, p["session_id"], "permission", detail, event_at=_EVENT_AT)
    if not _holds_prompts(conn, p["session_id"]):
        return None
    answer = _hold(conn, p["session_id"], {"kind": "permission", "tool": p.get("tool_name"), "detail": detail})
    if answer is None:
        return None
    if answer.get("behavior") == "allow":
        decision = {"behavior": "allow"}
    else:
        decision = {"behavior": "deny", **({"message": answer["message"]} if answer.get("message") else {})}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


# Notification types that mean the session has stopped mid-turn on the human.
_NOTIFICATION_WAITS = {
    "permission_prompt": "permission",
    "elicitation_dialog": "input",
    "elicitation_url_dialog": "input",
    "agent_needs_input": "input",
}


def _notification(p: dict) -> None:
    notification_type = p.get("notification_type") or ""
    if notification_type.startswith("quota_auto_resume_"):
        db.session_quota_notification(db.get_connection(), p["session_id"], notification_type)
        return
    kind = _NOTIFICATION_WAITS.get(notification_type)
    if kind:
        db.session_waiting(db.get_connection(), p["session_id"], kind, p.get("message"), event_at=_EVENT_AT)


def _pretool(p: dict) -> dict | None:
    """Registered only for the tools that hand the turn to the human (see
    installer.SESSION_HOOKS), so this isn't a process per tool call. For a background
    session it holds the turn for the dashboard: a question is answered by returning
    the tool's input with `answers` filled in, a plan by allowing or denying it."""
    tool, tool_input = p.get("tool_name"), p.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "AskUserQuestion":
        questions = tool_input.get("questions") if isinstance(tool_input.get("questions"), list) else []
        first = questions[0] if questions and isinstance(questions[0], dict) else None
        detail = first.get("question") if first else None
        prompt = {"kind": "question", "detail": detail, "questions": questions}
    elif tool == "ExitPlanMode":
        detail = "A plan is ready for your approval"
        prompt = {"kind": "plan", "detail": detail, "plan": tool_input.get("plan")}
    else:
        return None
    conn = db.get_connection()
    db.session_waiting(conn, p["session_id"], "input", detail, event_at=_EVENT_AT)
    if not _holds_prompts(conn, p["session_id"]):
        return None
    answer = _hold(conn, p["session_id"], prompt)
    if answer is None:
        return None
    out = {"hookEventName": "PreToolUse"}
    if tool == "AskUserQuestion" and isinstance(answer.get("answers"), dict):
        out.update(permissionDecision="allow", updatedInput={**tool_input, "answers": answer["answers"]})
    elif tool == "ExitPlanMode" and answer.get("behavior") == "allow":
        out.update(permissionDecision="allow", updatedInput=tool_input)
    elif answer.get("behavior") == "deny":
        out.update(permissionDecision="deny", permissionDecisionReason=answer.get("message") or "Not approved.")
    else:
        return None
    return {"hookSpecificOutput": out}


def _prompt(p: dict) -> None:
    from apo import transcripts

    prompt = p.get("prompt")
    conn = db.get_connection()
    db.session_prompt_submitted(
        conn, p["session_id"], transcripts.clean_prompt(prompt) if isinstance(prompt, str) else None,
        p.get("cwd"), event_at=_EVENT_AT,
    )
    answers = db.take_answers_for_session(conn, p["session_id"])
    if not answers:
        return None
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": _answers_context(answers)}}


def _answers_context(answers: list[dict]) -> str:
    """Information, not an order: pickup stays the user's call."""
    parts = ["The human has answered requests this session filed:"]
    for d in answers:
        record = f"\nFull record: {d['record_path']}" if d.get("record_path") else ""
        parts.append(f'\n#{d["id"]} ("{d["title"]}"): {d["answer_text"]}{record}')
    ids = ", ".join(f"mark_decision_resolved({d['id']})" for d in answers)
    parts.append(
        "\nIf the user's message is about continuing that work, apply the answer, then call "
        f"{ids} once it's done. Otherwise, carry on with what the user asked."
    )
    return "\n".join(parts)


def _cmd_session_heartbeat(args: argparse.Namespace) -> None:
    _run_hook(_heartbeat)


def _stop(p: dict) -> None:
    from apo import transcripts

    conn = db.get_connection()
    db.session_stop(conn, p["session_id"])
    info = transcripts.tail_info(p.get("transcript_path"))
    if info["away_summary"]:
        summary, at = info["away_summary"], info["away_summary_at"]
    else:
        summary, at = p.get("last_assistant_message") or info["last_assistant"], None
    db.record_session_summary(
        conn, p["session_id"], summary, title=info["title"], last_prompt=info["last_prompt"], at=at,
        context_tokens=info["context_tokens"],
    )


def _cmd_session_stop(args: argparse.Namespace) -> None:
    _run_hook(_stop)


def _cmd_backfill_summaries(args: argparse.Namespace) -> None:
    from apo import transcripts

    conn = db.get_connection()
    filled = 0
    for s in db.list_sessions(conn, include_ended=True):
        matches = list(Path(args.projects_dir).glob(f"*/{s['claude_session_id']}.jsonl"))
        if not matches:
            continue
        info = transcripts.tail_info(str(matches[0]))
        if info["away_summary"] or info["last_assistant"] or info["title"] or info["last_prompt"]:
            modified = datetime.fromtimestamp(matches[0].stat().st_mtime, timezone.utc).isoformat()
            db.record_session_summary(
                conn, s["claude_session_id"], info["away_summary"] or info["last_assistant"],
                title=info["title"], last_prompt=info["last_prompt"], at=modified,
            )
            filled += 1
    print(f"Backfilled {filled} session(s).")


def _cmd_session_end(args: argparse.Namespace) -> None:
    _run_hook(lambda p: db.session_end(db.get_connection(), p["session_id"], p.get("reason")))


def _cmd_checklist_item_created(args: argparse.Namespace) -> None:
    def create(p):
        # Claude Code sends task_subject; task_title was this handler's original
        # (wrong) guess and is still accepted.
        title = p.get("task_subject") or p.get("task_title")
        if p.get("task_id") and title:
            db.record_checklist_item(
                db.get_connection(), p["session_id"], str(p["task_id"]), title, p.get("task_description"),
            )
    _run_hook(create)


def _cmd_checklist_item_completed(args: argparse.Namespace) -> None:
    def complete(p):
        if p.get("task_id"):
            db.complete_checklist_item(
                db.get_connection(), p["session_id"], str(p["task_id"]), p.get("completion_notes"),
            )
    _run_hook(complete)


def _cmd_settings(args: argparse.Namespace) -> None:
    from apo.integrations import sources
    conn = db.get_connection()
    if args.enable:
        db.set_connector_enabled(conn, args.enable, True)
        print(f"Enabled '{args.enable}'.")
        return
    if args.disable:
        db.set_connector_enabled(conn, args.disable, False)
        print(f"Disabled '{args.disable}'.")
        return
    for src in sources.SOURCES:
        configured = "configured" if src.is_configured() else "not configured"
        enabled = "enabled" if db.is_connector_enabled(conn, src.name) else "disabled"
        print(f"{src.name}: {configured}, {enabled}")


def _cmd_sync_errors(args: argparse.Namespace) -> None:
    conn = db.get_connection()
    rows = db.recent_sync_errors(conn, limit=args.limit)
    if not rows:
        print("No sync errors recorded.")
        return
    for row in rows:
        repeats = f" ×{row['count']} since {row['first_occurred_at']}" if row["count"] > 1 else ""
        print(f"[{row['occurred_at']}] {row['source']}" + (f" (decision #{row['decision_request_id']})" if row["decision_request_id"] else "") + repeats + (f" [resolved {row['resolved_at']}]" if row["resolved_at"] else ""))
        print(f"    {row['message']}")
        print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="apo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"apo {apo.__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="File a new decision request")
    ask.add_argument("--project", required=True)
    ask.add_argument("--title", required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--summary", default=None, help="One or two sentences a human can read in five seconds")
    ask.add_argument("--context", default=None)
    ask.add_argument("--why-it-matters", default=None)
    ask.add_argument("--recommendation", default=None)
    ask.add_argument("--confidence", type=int, default=None)
    ask.add_argument("--blocked-work", default=None, help="JSON list of strings")
    ask.add_argument("--safe-work", default=None, help="JSON list of strings")
    ask.add_argument(
        "--session-id", default=None,
        help="Defaults to $CLAUDE_CODE_SESSION_ID — provenance for which session asked.",
    )
    ask.set_defaults(func=_cmd_ask)

    act = sub.add_parser("act", help="File a new action request — something only a human can go do")
    act.add_argument("--project", required=True)
    act.add_argument("--title", required=True)
    act.add_argument("--question", required=True)
    act.add_argument("--summary", default=None, help="One or two sentences a human can read in five seconds")
    act.add_argument("--context", default=None)
    act.add_argument("--why-it-matters", default=None)
    act.add_argument("--blocked-work", default=None, help="JSON list of strings")
    act.add_argument("--safe-work", default=None, help="JSON list of strings")
    act.add_argument(
        "--session-id", default=None,
        help="Defaults to $CLAUDE_CODE_SESSION_ID — provenance for which session asked.",
    )
    act.set_defaults(func=_cmd_act)

    bug = sub.add_parser("bug", help="Report a bug: a task for the project's agents, labelled bug")
    bug.add_argument("title", help="What's wrong, in a line")
    bug.add_argument("--project", default=None, help="Defaults to the project of the folder you're in")
    bug.add_argument("--happened", default=None, help="What happened")
    bug.add_argument("--expected", default=None, help="What should have happened")
    bug.add_argument("--steps", default=None, help="Steps to reproduce")
    bug.add_argument("--severity", choices=db.BUG_SEVERITIES, default="normal")
    bug.set_defaults(func=_cmd_bug)

    status_p = sub.add_parser("status", help="A status you can paste: done, in progress, waiting on you, stuck, next")
    status_p.add_argument("--project", default=None, help="One project (default: every active project)")
    status_p.add_argument("--since", default="7d", help="7d, 2w or YYYY-MM-DD (default 7d)")
    status_p.add_argument("--format", choices=["md", "json"], default="md")
    status_p.set_defaults(func=_cmd_status)

    ans = sub.add_parser("answer", help="Answer a decision request and write it into its repo")
    ans.add_argument("decision_id", type=int)
    ans.add_argument("--answer", required=True, dest="answer_text")
    ans.set_defaults(func=_cmd_answer)

    repo = sub.add_parser("repo-add", help="Register a repository so its decisions get written there")
    repo.add_argument("--project", required=True)
    repo.add_argument("--path", required=True, help="Local filesystem path to write .claude/decisions/ into")
    repo.add_argument("--name", default=None, help="Defaults to --project")
    repo.set_defaults(func=_cmd_repo_add)

    lst = sub.add_parser("list", help="List decision requests")
    lst.add_argument("--project", default=None)
    lst.add_argument("--status", choices=["open", "answered", "resolved"], default=None)
    lst.add_argument("--search", default=None, help="Substring match against title/question/context")
    lst.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")
    lst.set_defaults(func=_cmd_list)

    show = sub.add_parser("show", help="Show full detail for one decision request")
    show.add_argument("decision_id", type=int)
    show.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")
    show.set_defaults(func=_cmd_show)

    archive = sub.add_parser(
        "archive-project",
        help="Archive a completed project: hide it from the dashboard's default view and "
        "force-close any still-open decisions, without deleting any records",
    )
    archive.add_argument("--project", required=True)
    archive.add_argument(
        "--note", default="Closed — project archived.",
        help="Recorded as the answer for any decision that gets force-closed without a real answer",
    )
    archive.set_defaults(func=_cmd_archive_project)

    projects_p = sub.add_parser(
        "projects", help="List projects with their folders and aliases, and flag folders shared by two projects",
    )
    projects_p.set_defaults(func=_cmd_projects)

    merge = sub.add_parser(
        "project-merge",
        help="Fold one project into another (decisions, sessions, folders); the old name becomes an alias",
    )
    merge.add_argument("--from", dest="from_project", required=True)
    merge.add_argument("--into", dest="into_project", required=True)
    merge.set_defaults(func=_cmd_project_merge)

    settings_p = sub.add_parser(
        "settings", help="List connectors and whether sync is enabled, or toggle one",
    )
    settings_p.add_argument("--enable", metavar="NAME", default=None, help="Turn sync on for this connector")
    settings_p.add_argument("--disable", metavar="NAME", default=None, help="Turn sync off for this connector")
    settings_p.set_defaults(func=_cmd_settings)

    errs = sub.add_parser("sync-errors", help="Show recent sync errors (Linear push/poll/resolve failures, dropped follow-up comments)")
    errs.add_argument("--limit", type=int, default=20)
    errs.set_defaults(func=_cmd_sync_errors)

    pending = sub.add_parser(
        "pending",
        help="List answered decisions for a repository that haven't been worked yet "
        "(the DB-side view of the ANSWERED rows in .claude/decisions/INDEX.md)",
    )
    pending.add_argument("--path", default=None, help="Repository path to check, defaults to cwd")
    pending.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")
    pending.add_argument("--all", action="store_true",
                         help="Every project: answered items and READY tasks, with the folder to work them in")
    pending.set_defaults(func=_cmd_pending)

    tasks_p = sub.add_parser("tasks", help="List agent tasks (READY / IN PROGRESS) for a repository")
    tasks_p.add_argument("--path", default=None, help="Repository path to check, defaults to cwd")
    tasks_p.add_argument("--json", action="store_true")
    tasks_p.set_defaults(func=_cmd_tasks)

    block_p = sub.add_parser("block", help="Make a task wait on others (--on), or stop waiting (--remove)")
    block_p.add_argument("task_id", type=int)
    block_p.add_argument("--on", type=int, action="append", default=[], metavar="ID", help="Blocker id (repeatable)")
    block_p.add_argument("--remove", type=int, action="append", default=[], metavar="ID", help="Blocker id to drop")
    block_p.set_defaults(func=_cmd_block)

    claim_p = sub.add_parser("claim", help="Claim a READY task before working it (the claim_task tool, from a shell)")
    claim_p.add_argument("task_id", type=int)
    claim_p.set_defaults(func=_cmd_claim)

    complete_p = sub.add_parser("complete", help="Mark a task done (the complete_task tool, from a shell)")
    complete_p.add_argument("task_id", type=int)
    complete_p.add_argument("--note", default=None, help="What you did")
    complete_p.add_argument("--outcome", choices=["done", "partial"], default=None,
                            help="partial keeps it flagged as partly done")
    complete_p.add_argument("--done", default=None, help="What got done")
    complete_p.add_argument("--not-done", dest="not_done", default=None, help="What is still not done")
    complete_p.add_argument("--left-for-human", dest="left_for_human", default=None, help="What only the user can do")
    complete_p.add_argument("--branch", default=None, help="Where the work is")
    complete_p.set_defaults(func=_cmd_complete)

    note_p = sub.add_parser("note", help="Add a note to an item's thread (the add_note tool, from a shell)")
    note_p.add_argument("item_id", type=int)
    note_p.add_argument("text")
    note_p.set_defaults(func=_cmd_note)

    retry_p = sub.add_parser("retry", help="Put a failed, abandoned or stuck-claimed task back to READY")
    retry_p.add_argument("task_id", type=int)
    retry_p.add_argument("--note", default=None, help="Why")
    retry_p.set_defaults(func=_cmd_retry)

    stuck_p = sub.add_parser("stuck", help="Claimed tasks whose session has ended (retry them)")
    stuck_p.add_argument("--path", default=None, help="Repository path, defaults to cwd")
    stuck_p.set_defaults(func=_cmd_stuck)

    move_p = sub.add_parser("move", help="Put an item in another of its project's folders (so that folder's agents see it)")
    move_p.add_argument("item_id", type=int)
    move_p.add_argument("folder", help="A folder path, or its name as `apo projects` shows it")
    move_p.set_defaults(func=_cmd_move)

    resolve = sub.add_parser(
        "resolve", help="Mark an answered decision resolved once its work is done",
    )
    resolve.add_argument("decision_id", type=int)
    resolve.set_defaults(func=_cmd_resolve)

    init = sub.add_parser(
        "init",
        help="Set up apo for Claude Code: the data folder, the MCP server, the hooks and the /decisions skill",
    )
    init.add_argument("--yes", action="store_true", help="Non-interactive: use flags/defaults, don't prompt")
    init.add_argument("--db-path", default=None, help="Where to keep the database (default: the data folder)")
    init.add_argument("--projects-root", default=None, help="Where your projects live (default ~/projects)")
    init.add_argument("--linear-api-key", default=None)
    init.add_argument("--linear-team", default=None)
    init.add_argument("--skip-linear", action="store_true", help="Don't ask about Linear")
    init.add_argument("--skip-mcp", action="store_true", help="Don't register the MCP server")
    init.add_argument("--skip-hook", action="store_true", help="Don't add the hooks")
    init.add_argument("--skip-skill", action="store_true", help="Don't install the /decisions skill")
    init.set_defaults(func=_cmd_init)

    dashboard = sub.add_parser(
        "dashboard", help="Start, stop or check the dashboard; `run` serves it in the foreground (for a service)",
    )
    dashboard.add_argument("action", choices=("start", "stop", "status", "run"))
    dashboard.add_argument("--port", type=int, default=None, help="Default: APO_DASHBOARD_PORT, else 8430")
    dashboard.add_argument("--dev", action="store_true", help="Reload when apo's own code changes (for working on apo)")
    dashboard.set_defaults(func=_cmd_dashboard)

    mcp = sub.add_parser("mcp", help="Run the MCP server on stdio (Claude Code starts this itself)",
                         description="Run the MCP server on stdio. Claude Code starts this itself once `apo init` has registered it.")
    mcp.set_defaults(func=_cmd_mcp)

    backfill = sub.add_parser(
        "backfill-summaries",
        help="Fill in each tracked session's last-done summary from its Claude Code transcript",
    )
    backfill.add_argument("--projects-dir", default=str(Path.home() / ".claude" / "projects"))
    backfill.set_defaults(func=_cmd_backfill_summaries)

    for name, help_text, func in (
        ("session-start", "Claude Code SessionStart hook target", _cmd_session_start),
        ("session-heartbeat", "Claude Code PostToolUse hook target", _cmd_session_heartbeat),
        ("session-stop", "Claude Code Stop hook target", _cmd_session_stop),
        ("session-end", "Claude Code SessionEnd hook target", _cmd_session_end),
        ("checklist-item-created", "Claude Code TaskCreated hook target", _cmd_checklist_item_created),
        ("checklist-item-completed", "Claude Code TaskCompleted hook target", _cmd_checklist_item_completed),
        ("session-prompt", "Claude Code UserPromptSubmit hook target", lambda a: _run_hook(_prompt)),
        ("session-notification", "Claude Code Notification hook target", lambda a: _run_hook(_notification)),
        ("session-permission", "Claude Code PermissionRequest hook target (holds a background session's prompt for the dashboard)",
         lambda a: _run_hook(_permission)),
        ("session-pretool", "Claude Code PreToolUse hook target (AskUserQuestion/ExitPlanMode)",
         lambda a: _run_hook(_pretool)),
        ("session-stop-failure", "Claude Code StopFailure hook target",
         lambda a: _run_hook(lambda p: db.session_stop_failure(
             db.get_connection(), p["session_id"], p.get("error") or p.get("error_type"),
             message=" ".join(str(p.get(k) or "") for k in ("last_assistant_message", "error_details")).strip() or None))),
    ):
        hook = sub.add_parser(name, help=f"{help_text} — reads its JSON payload from stdin, always exits 0")
        hook.set_defaults(func=func)

    args = parser.parse_args(argv)
    args.func(args)


def legacy_main() -> None:
    """`ado`, the old name, kept for a release or two so existing hooks and habits keep
    working. The notice goes to stderr: a hook's stdout is what Claude Code reads."""
    print("ado is deprecated and will be removed; use apo instead.", file=sys.stderr)
    main()


if __name__ == "__main__":
    main()
