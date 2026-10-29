import subprocess
import sys
from pathlib import Path



def run_cli(*args, env, cwd=None, stdin=None):
    """cwd defaults to the test's own cwd, which conftest's autouse fixture has already
    pointed at a tmp_path — `ask` registers a repository from cwd and repo_writer then
    writes markdown into it, so inheriting this process's real cwd would scribble into
    the orchestrator checkout."""
    return subprocess.run(
        [sys.executable, "-m", "apo", *args], capture_output=True, text=True, env=env, cwd=cwd, input=stdin,
    )


def test_ask_then_list(db_path, monkeypatch):
    import os
    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    ask = run_cli(
        "ask", "--project", "acme", "--title", "Clarify mobile nav",
        "--question", "Bottom tabs, drawer, or sidebar?",
        env=env,
    )
    assert ask.returncode == 0, ask.stderr
    assert "Filed decision request #" in ask.stdout

    listing = run_cli("list", "--project", "acme", "--status", "open", env=env)
    assert listing.returncode == 0, listing.stderr
    assert "Clarify mobile nav" in listing.stdout


def test_act_files_an_action_request(db_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    act = run_cli(
        "act", "--project", "acme", "--title", "Grant S3 access",
        "--question", "Please grant write access to the deploy bucket.",
        env=env,
    )
    assert act.returncode == 0, act.stderr
    assert "Filed action request #" in act.stdout

    conn = db.get_connection()
    row = db.list_decision_requests(conn, "acme")[0]
    assert row["kind"] == "action"


def test_ask_accepts_summary(db_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    ask = run_cli(
        "ask", "--project", "acme", "--title", "Q", "--question", "Q?",
        "--summary", "Short human-readable summary.",
        env=env,
    )
    assert ask.returncode == 0, ask.stderr

    conn = db.get_connection()
    rows = db.list_decision_requests(conn, "acme")
    assert rows[0]["summary"] == "Short human-readable summary."


def test_ask_auto_links_claude_code_session(db_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)
    env["CLAUDE_CODE_SESSION_ID"] = "cli-env-session"

    ask = run_cli("ask", "--project", "acme", "--title", "Q", "--question", "Q?", env=env)
    assert ask.returncode == 0, ask.stderr

    conn = db.get_connection()
    rows = db.list_decision_requests(conn, "acme")
    assert rows[0]["origin_session_id"] == "cli-env-session"


def test_projects_command_flags_folders_shared_by_two_projects(db_path, tmp_path):
    import os

    from apo import db

    conn = db.get_connection()
    for name in ("Umbrella", "Hooli"):
        project = db.get_or_create_project(conn, name)
        db.create_repository(conn, project["id"], name, str(tmp_path))

    result = run_cli("projects", env=dict(os.environ, APO_DB=str(db_path)))

    assert result.returncode == 0, result.stderr
    assert "Umbrella" in result.stdout and "Hooli" in result.stdout
    assert "share" in result.stdout.lower()
    assert "project-merge" in result.stdout


def test_project_merge_command_folds_one_project_into_another(db_path, tmp_path):
    import os

    from apo import db

    conn = db.get_connection()
    old = db.get_or_create_project(conn, "Umbrella")
    db.create_repository(conn, old["id"], "ts", str(tmp_path))
    decision_id = db.create_decision_request(conn, project_id=old["id"], title="Q", question="Q?")
    new = db.get_or_create_project(conn, "Hooli")
    env = dict(os.environ, APO_DB=str(db_path))

    result = run_cli("project-merge", "--from", "Umbrella", "--into", "Hooli", env=env)

    assert result.returncode == 0, result.stderr
    assert "1 decision" in result.stdout
    conn = db.get_connection()
    assert db.get_decision_request(conn, decision_id)["project_id"] == new["id"]

    missing = run_cli("project-merge", "--from", "Nope", "--into", "Hooli", env=env)
    assert missing.returncode != 0
    assert "Nope" in missing.stderr


def test_ask_auto_registers_repository_from_cwd(db_path, tmp_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    ask = run_cli(
        "ask", "--project", "acme", "--title", "Q", "--question", "Q?", env=env, cwd=str(tmp_path),
    )
    assert ask.returncode == 0, ask.stderr

    conn = db.get_connection()
    decision = db.list_decision_requests(conn, "acme")[0]
    assert decision["repository_id"] is not None
    repo = db.get_repository(conn, decision["repository_id"])
    assert repo["local_path"] == str(tmp_path)


def test_answer_records_and_writes_into_the_repo(db_path, tmp_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    repo_path = tmp_path / "checkout"
    repo_path.mkdir()
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", str(repo_path))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?",
    )

    answer = run_cli("answer", str(decision_id), "--answer", "Use drawer navigation.", env=env)
    assert answer.returncode == 0, answer.stderr
    assert str(repo_path) in answer.stdout

    decision = db.get_decision_request(conn, decision_id)
    assert decision["answer_text"] == "Use drawer navigation."
    assert decision["answer_source"] == "cli"
    assert decision["status"] == "answered"
    assert "Use drawer navigation." in (
        repo_path / ".claude" / "decisions" / f"{decision_id:04d}-q.md"
    ).read_text()

    # Answering again is a no-op, not an error.
    second = run_cli("answer", str(decision_id), "--answer", "irrelevant", env=env)
    assert second.returncode == 0
    assert "already answered" in second.stdout.lower()


def test_answer_without_a_repository_says_so_rather_than_failing_silently(db_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "no-repo")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    answer = run_cli("answer", str(decision_id), "--answer", "An answer", env=env)
    assert answer.returncode == 0, answer.stderr
    assert "no repository registered" in answer.stdout.lower()
    assert db.get_decision_request(conn, decision_id)["status"] == "answered"


def test_pending_lists_answered_not_resolved_decisions_for_repo(db_path, tmp_path):
    import json
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", str(tmp_path))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?",
    )
    db.answer_decision_request(conn, decision_id, "Go ahead.")

    clean = run_cli("pending", "--path", "/some/unrelated/path", "--json", env=env)
    assert clean.returncode == 0
    assert json.loads(clean.stdout) == []

    pending = run_cli("pending", "--path", str(tmp_path), "--json", env=env)
    assert pending.returncode == 0
    result = json.loads(pending.stdout)
    assert [r["id"] for r in result] == [decision_id]
    assert result[0]["answer_text"] == "Go ahead."

    db.resolve_decision_request(conn, decision_id)
    resolved = run_cli("pending", "--path", str(tmp_path), "--json", env=env)
    assert json.loads(resolved.stdout) == []


def test_archive_project_command_closes_open_decisions_and_hides_project(db_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    open_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    result = run_cli("archive-project", "--project", "acme", env=env)
    assert result.returncode == 0, result.stderr
    assert "Archived project 'acme'" in result.stdout
    assert f"#{open_id}" in result.stdout

    decision = db.get_decision_request(conn, open_id)
    assert decision["status"] == "resolved"
    assert db.project_counts(conn)["acme"]["archived"] is True

    again = run_cli("archive-project", "--project", "acme", env=env)
    assert "already archived" in again.stdout.lower()


def test_show_prints_full_decision_detail(db_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Which auth provider?",
        question="Auth0, Clerk, or roll our own?",
        context="Signup flow is otherwise done.",
        why_it_matters="Blocks launch.",
        options=["Auth0", "Clerk"],
        recommendation="Clerk", confidence=80,
        blocked_work=["Wire callback"], safe_work=["Write tests"],
        resume_plan={"objective": "Finish signup", "remaining_steps": ["Wire callback"]},
    )
    db.answer_decision_request(conn, decision_id, "Use Clerk.", source="cli")

    show = run_cli("show", str(decision_id), env=env)
    assert show.returncode == 0, show.stderr
    assert "Which auth provider?" in show.stdout
    assert "Auth0, Clerk, or roll our own?" in show.stdout
    assert "Signup flow is otherwise done." in show.stdout
    assert "Blocks launch." in show.stdout
    assert "Auth0" in show.stdout and "Clerk" in show.stdout
    assert "confidence 80" in show.stdout
    assert "Wire callback" in show.stdout
    assert "Write tests" in show.stdout
    assert "Use Clerk." in show.stdout
    assert "Finish signup" in show.stdout


def test_show_json(db_path):
    import json
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    show = run_cli("show", str(decision_id), "--json", env=env)
    assert show.returncode == 0, show.stderr
    result = json.loads(show.stdout)
    assert result["id"] == decision_id
    assert result["title"] == "Q"


def test_show_unknown_id_says_so_rather_than_failing_silently(db_path):
    import os
    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    show = run_cli("show", "999", env=env)
    assert show.returncode == 0
    assert "no decision request" in show.stdout.lower()


def test_list_search_filters_by_text(db_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.create_decision_request(conn, project_id=project["id"], title="Auth provider", question="Q?")
    db.create_decision_request(conn, project_id=project["id"], title="Database choice", question="Q?")

    listing = run_cli("list", "--search", "Auth", env=env)
    assert listing.returncode == 0, listing.stderr
    assert "Auth provider" in listing.stdout
    assert "Database choice" not in listing.stdout


def test_list_json(db_path):
    import json
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    listing = run_cli("list", "--json", env=env)
    assert listing.returncode == 0, listing.stderr
    result = json.loads(listing.stdout)
    assert result[0]["id"] == decision_id


def test_init_command_is_wired_up_and_non_interactive_with_yes(db_path):
    import os

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    result = run_cli(
        "init", "--yes", "--skip-linear", "--skip-mcp", "--skip-hook", "--skip-skill", env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "apo init complete" in result.stdout.lower()


def test_settings_command_lists_connectors(db_path):
    import os

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    result = run_cli("settings", env=env)
    assert result.returncode == 0, result.stderr
    assert "linear" in result.stdout.lower()


def test_settings_command_disables_and_enables_a_connector(db_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    off = run_cli("settings", "--disable", "linear", env=env)
    assert off.returncode == 0, off.stderr
    conn = db.get_connection()
    assert db.is_connector_enabled(conn, "linear") is False

    on = run_cli("settings", "--enable", "linear", env=env)
    assert on.returncode == 0, on.stderr
    assert db.is_connector_enabled(conn, "linear") is True


def test_resolve_command_marks_answered_decision_resolved(db_path):
    import os

    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")

    too_early = run_cli("resolve", str(decision_id), env=env)
    assert too_early.returncode == 0
    assert "not in an answered state" in too_early.stdout.lower()

    db.answer_decision_request(conn, decision_id, "An answer")
    resolved = run_cli("resolve", str(decision_id), env=env)
    assert resolved.returncode == 0
    assert "resolved" in resolved.stdout.lower()
    assert db.get_decision_request(conn, decision_id)["status"] == "resolved"


def test_pending_all_says_where_answers_wait_and_how_to_work_them(db_path, tmp_path):
    import json
    import os

    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    conn = db.get_connection()
    for name in ("acme", "beta"):
        folder = tmp_path / name
        folder.mkdir()
        project = db.get_or_create_project(conn, name)
        repo = db.create_repository(conn, project["id"], name, str(folder))
        decision_id = db.create_decision_request(conn, project_id=project["id"], repository_id=repo["id"],
                                                 title=f"{name} question", question="Q?")
        db.answer_decision_request(conn, decision_id, "Go ahead.")

    text = run_cli("pending", "--all", env=env).stdout
    assert "acme question" in text and "beta question" in text
    assert f'cd {tmp_path / "acme"} && claude "/decisions"' in text

    data = json.loads(run_cli("pending", "--all", "--json", env=env).stdout)
    assert {d["project"] for d in data} == {"acme", "beta"}
    assert all(d["path"] for d in data)


def test_tasks_can_be_listed_claimed_and_completed_from_the_cli(db_path, tmp_path):
    """The fallback for a session whose MCP tool list predates claim_task/complete_task
    (a resumed session keeps the tools it started with)."""
    import json
    import os

    from apo import db

    env = dict(os.environ, APO_DB=str(db_path), CLAUDE_CODE_SESSION_ID="sess-cli")
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))
    task_id = db.create_task(conn, project_id=project["id"], title="Fix the scroll bar")

    listed = json.loads(run_cli("tasks", "--path", str(tmp_path), "--json", env=env).stdout)
    assert [t["id"] for t in listed] == [task_id]

    claimed = run_cli("claim", str(task_id), env=env)
    assert claimed.returncode == 0 and "Claimed" in claimed.stdout
    assert db.get_decision_request(conn, task_id)["claimed_by_session_id"] == "sess-cli"
    assert run_cli("claim", str(task_id), env=env).returncode == 1

    done = run_cli("complete", str(task_id), "--note", "Fixed overflow on iOS standalone", env=env)
    assert done.returncode == 0
    task = db.get_decision_request(conn, task_id)
    assert task["status"] == "resolved" and "iOS standalone" in task["answer_text"]
    index = (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text().splitlines()
    assert not [line for line in index if line.startswith("| READY |") and "Fix the scroll bar" in line]


def test_bug_files_a_task_labelled_bug_in_this_folders_project(db_path, tmp_path):
    import os

    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    db.ensure_repository_for_project(conn, project["id"], str(tmp_path))

    filed = run_cli("bug", "Resume fires an hour early", "--happened", "Asked for 16:00, it says 15:00",
                    "--expected", "16:00", "--steps", "Schedule a resume the evening before a clock change",
                    "--severity", "urgent", env=env, cwd=str(tmp_path))

    assert filed.returncode == 0, filed.stderr
    bug = db.list_tasks(conn, project["id"])[0]
    assert f"#{bug['id']}" in filed.stdout and "acme" in filed.stdout
    assert bug["title"] == "Resume fires an hour early" and bug["labels"] == ["bug", "urgent"]
    assert "Asked for 16:00" in bug["question"] and "Expected: 16:00" in bug["question"]
    assert "evening before a clock change" in bug["question"]
    index = (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()
    assert "| READY |" in index and "Resume fires an hour early" in index


def test_bug_takes_a_project_by_name_from_anywhere(db_path, tmp_path):
    import os

    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = tmp_path / "acme"
    repo.mkdir()
    db.ensure_repository_for_project(conn, project["id"], str(repo))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    filed = run_cli("bug", "Login button does nothing", "--project", "acme", env=env, cwd=str(elsewhere))

    assert filed.returncode == 0, filed.stderr
    assert [t["title"] for t in db.list_tasks(conn, project["id"])] == ["Login button does nothing"]
    assert "Login button does nothing" in (repo / ".claude" / "decisions" / "INDEX.md").read_text()


def test_bug_outside_a_project_folder_needs_a_project_name(db_path, tmp_path):
    import os

    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))

    filed = run_cli("bug", "Something broke", env=env, cwd=str(tmp_path))

    assert filed.returncode == 1 and "--project" in filed.stderr
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM decision_requests").fetchone()[0] == 0
