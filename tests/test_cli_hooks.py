"""The six Claude Code hook-target subcommands (session-start, session-heartbeat,
session-stop, session-end, checklist-item-created, checklist-item-completed).

Every one of these MUST be fail-open: Claude Code execs them as real hooks, and
`session-stop` in particular can BLOCK the agent's turn if it exits nonzero or emits
a blocking decision. A bug here has a much higher blast radius than a normal CLI
command failing loudly, so the fail-open contract gets its own dedicated coverage
independent of the happy-path behavior (already covered in tests/test_sessions.py at
the db.py layer).
"""
import json
import os
import subprocess
import sys
from pathlib import Path


HOOK_COMMANDS = (
    "session-start", "session-heartbeat", "session-stop", "session-end",
    "checklist-item-created", "checklist-item-completed",
    "session-prompt", "session-notification", "session-permission", "session-pretool",
    "session-stop-failure",
)


def run_cli(*args, env, cwd=None, stdin=None):
    return subprocess.run(
        [sys.executable, "-m", "apo", *args], capture_output=True, text=True, env=env, cwd=cwd, input=stdin,
    )


def test_all_hook_commands_exit_zero_and_print_empty_json_on_empty_stdin(db_path):
    env = dict(os.environ)
    env["APO_DB"] = str(db_path)
    for cmd in HOOK_COMMANDS:
        result = run_cli(cmd, env=env, stdin="")
        assert result.returncode == 0, f"{cmd}: {result.stderr}"
        assert result.stdout.strip() == "{}", f"{cmd}: {result.stdout!r}"


def test_all_hook_commands_exit_zero_on_malformed_json(db_path):
    env = dict(os.environ)
    env["APO_DB"] = str(db_path)
    for cmd in HOOK_COMMANDS:
        result = run_cli(cmd, env=env, stdin="not json at all {{{")
        assert result.returncode == 0, f"{cmd}: {result.stderr}"
        assert result.stdout.strip() == "{}"


def test_all_hook_commands_exit_zero_when_session_id_missing(db_path):
    env = dict(os.environ)
    env["APO_DB"] = str(db_path)
    for cmd in HOOK_COMMANDS:
        result = run_cli(cmd, env=env, stdin=json.dumps({"cwd": "/tmp", "hook_event_name": cmd}))
        assert result.returncode == 0, f"{cmd}: {result.stderr}"
        assert result.stdout.strip() == "{}"


def test_all_hook_commands_exit_zero_even_when_db_is_unreachable(db_path, tmp_path):
    env = dict(os.environ)
    bad_db = tmp_path / "not-a-file"
    bad_db.mkdir()
    env["APO_DB"] = str(bad_db)  # a directory, not a file — sqlite3.connect raises

    payload = json.dumps({"session_id": "sess-1", "cwd": str(tmp_path), "task_id": "t1", "task_title": "x"})
    for cmd in HOOK_COMMANDS:
        result = run_cli(cmd, env=env, stdin=payload)
        assert result.returncode == 0, f"{cmd}: {result.stderr}"
        assert result.stdout.strip() == "{}"


def test_session_start_hook_registers_session(db_path, tmp_path):
    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)
    payload = json.dumps({"session_id": "sess-1", "cwd": str(tmp_path), "source": "startup"})

    result = run_cli("session-start", env=env, stdin=payload)

    assert result.returncode == 0
    conn = db.get_connection()
    sessions = db.list_sessions(conn)
    assert sessions[0]["claude_session_id"] == "sess-1"
    assert sessions[0]["status"] == "running"


def test_session_heartbeat_hook_updates_last_seen(db_path, tmp_path):
    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)
    run_cli("session-start", env=env, stdin=json.dumps({"session_id": "sess-1", "cwd": str(tmp_path)}))

    result = run_cli(
        "session-heartbeat", env=env,
        stdin=json.dumps({"session_id": "sess-1", "cwd": str(tmp_path), "tool_name": "Bash"}),
    )

    assert result.returncode == 0
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_session_stop_hook_marks_idle(db_path, tmp_path):
    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)
    run_cli("session-start", env=env, stdin=json.dumps({"session_id": "sess-1", "cwd": str(tmp_path)}))

    result = run_cli("session-stop", env=env, stdin=json.dumps({"session_id": "sess-1"}))

    assert result.returncode == 0
    conn = db.get_connection()
    assert db.list_sessions(conn)[0]["status"] == "idle"


def test_session_end_hook_marks_ended(db_path, tmp_path):
    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)
    run_cli("session-start", env=env, stdin=json.dumps({"session_id": "sess-1", "cwd": str(tmp_path)}))

    result = run_cli(
        "session-end", env=env, stdin=json.dumps({"session_id": "sess-1", "reason": "logout"}),
    )

    assert result.returncode == 0
    conn = db.get_connection()
    sessions = db.list_sessions(conn, include_ended=True)
    assert sessions[0]["status"] == "ended"
    assert sessions[0]["end_reason"] == "logout"


def test_checklist_item_created_and_completed_hooks(db_path, tmp_path):
    from apo import db

    env = dict(os.environ)
    env["APO_DB"] = str(db_path)
    run_cli("session-start", env=env, stdin=json.dumps({"session_id": "sess-1", "cwd": str(tmp_path)}))

    created = run_cli(
        "checklist-item-created", env=env,
        stdin=json.dumps({
            "session_id": "sess-1", "task_id": "task-1",
            "task_title": "Write tests", "task_description": "Cover the happy path",
        }),
    )
    assert created.returncode == 0

    conn = db.get_connection()
    session_id = db.list_sessions(conn)[0]["id"]
    items = db.list_checklist_items(conn, session_id)
    assert items[0]["title"] == "Write tests"
    assert items[0]["status"] == "pending"

    completed = run_cli(
        "checklist-item-completed", env=env,
        stdin=json.dumps({
            "session_id": "sess-1", "task_id": "task-1",
            "task_status": "completed", "completion_notes": "12 tests added.",
        }),
    )
    assert completed.returncode == 0

    item = db.list_checklist_items(conn, session_id)[0]
    assert item["status"] == "done"
    assert item["completed_by"] == "agent"
    assert item["completion_notes"] == "12 tests added."


def _start(env, tmp_path, session_id="sess-1"):
    run_cli("session-start", env=env, stdin=json.dumps({"session_id": session_id, "cwd": str(tmp_path)}))


def _items(session_index=0):
    from apo import db
    conn = db.get_connection()
    return db.list_checklist_items(conn, db.list_sessions(conn, include_ended=True)[session_index]["id"])


def test_checklist_item_created_accepts_real_task_subject_field(db_path, tmp_path):
    # Claude Code's TaskCreated payload carries task_subject, not task_title.
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    run_cli("checklist-item-created", env=env, stdin=json.dumps({
        "session_id": "sess-1", "hook_event_name": "TaskCreated", "task_id": "1",
        "task_subject": "Write tests", "task_description": "Cover the happy path",
    }))
    run_cli("checklist-item-completed", env=env, stdin=json.dumps({
        "session_id": "sess-1", "hook_event_name": "TaskCompleted", "task_id": "1",
        "task_subject": "Write tests",
    }))

    items = _items()
    assert [i["title"] for i in items] == ["Write tests"]
    assert items[0]["status"] == "done"


def _post_tool_use(env, tool_name, tool_input, tool_response):
    return run_cli("session-heartbeat", env=env, stdin=json.dumps({
        "session_id": "sess-1", "hook_event_name": "PostToolUse", "tool_name": tool_name,
        "tool_input": tool_input, "tool_response": tool_response,
    }))


def test_heartbeat_syncs_checklist_from_task_create_and_update(db_path, tmp_path):
    # TaskCreated doesn't fire for an ordinary session's TaskCreate, but PostToolUse does.
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    result = _post_tool_use(
        env, "TaskCreate", {"subject": "Fix bug", "description": "The login one"},
        {"task": {"id": "1", "subject": "Fix bug"}},
    )
    assert result.returncode == 0 and result.stdout.strip() == "{}"
    assert [(i["title"], i["status"]) for i in _items()] == [("Fix bug", "pending")]

    _post_tool_use(
        env, "TaskUpdate", {"taskId": "1", "status": "in_progress"},
        {"success": True, "taskId": "1", "statusChange": {"from": "pending", "to": "in_progress"}},
    )
    assert _items()[0]["status"] == "in_progress"

    _post_tool_use(
        env, "TaskUpdate", {"taskId": "1", "status": "completed"},
        {"success": True, "taskId": "1", "statusChange": {"from": "in_progress", "to": "completed"}},
    )
    assert _items()[0]["status"] == "done"


def _hook(env, cmd, **payload):
    return run_cli(cmd, env=env, stdin=json.dumps({"session_id": "sess-1", **payload}))


def _state():
    from apo import db
    return db.list_sessions(db.get_connection(), include_ended=True)[0]


def test_permission_request_hook_shows_what_is_waiting_for_permission(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    result = _hook(
        env, "session-permission", hook_event_name="PermissionRequest", tool_name="Bash",
        tool_input={"command": "stripe listen --forward-to localhost:8000/webhooks", "description": "x"},
    )

    assert result.returncode == 0 and result.stdout.strip() == "{}"
    s = _state()
    assert s["display_status"] == "waiting_permission"
    assert s["waiting_detail"] == "Bash: stripe listen --forward-to localhost:8000/webhooks"


def test_notification_hook_maps_types_to_waits(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    _hook(env, "session-notification", notification_type="permission_prompt",
          message="Claude needs your permission to use Bash")
    assert _state()["display_status"] == "waiting_permission"

    _hook(env, "session-notification", notification_type="elicitation_dialog", message="Pick a region")
    assert _state()["display_status"] == "waiting_input"
    assert _state()["waiting_detail"] == "Pick a region"

    _hook(env, "session-notification", notification_type="auth_success", message="ok")
    assert _state()["display_status"] == "waiting_input"


def test_pretool_hook_treats_a_question_to_the_user_as_waiting_for_input(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    _hook(env, "session-pretool", tool_name="AskUserQuestion",
          tool_input={"questions": [{"question": "Postgres or SQLite?", "options": []}]})
    s = _state()
    assert s["display_status"] == "waiting_input"
    assert s["waiting_detail"] == "Postgres or SQLite?"

    _hook(env, "session-pretool", tool_name="ExitPlanMode", tool_input={"plan": "..."})
    assert _state()["waiting_detail"] == "A plan is ready for your approval"


def test_prompt_hook_starts_a_turn_with_the_cleaned_prompt(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    _hook(env, "session-prompt", prompt="fix it <pasted_content id=\"x\">huge paste</pasted_content> please",
          cwd=str(tmp_path))

    s = _state()
    assert s["display_status"] == "running"
    assert s["last_prompt"] == "fix it [pasted] please"


def test_stop_failure_hook_records_the_error(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    # StopFailure's payload calls it "error" (with "error_details"); not "error_type".
    _hook(env, "session-stop-failure", error="overloaded", error_details="529 Overloaded")

    s = _state()
    assert s["display_status"] == "errored"
    assert s["last_error"] == "overloaded"


def test_a_usage_limit_stop_waits_for_claude_codes_auto_continue(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    _hook(env, "session-prompt", prompt="go")

    _hook(env, "session-stop-failure", error="rate_limit")
    assert _state()["display_status"] == "usage_limit"
    assert not _state()["needs_you"]

    _hook(env, "session-notification", notification_type="quota_auto_resume_fired", message="Usage limit reset")
    assert _state()["display_status"] == "running"


def test_a_usage_limit_stop_needs_you_when_auto_continue_is_off(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    _hook(env, "session-prompt", prompt="go")
    _hook(env, "session-stop-failure", error="rate_limit")

    _hook(env, "session-notification", notification_type="quota_auto_resume_disabled")

    s = _state()
    assert s["display_status"] == "errored" and s["needs_you"]


def test_heartbeat_binds_a_filed_request_to_the_session_that_actually_filed_it(db_path, tmp_path):
    """The PostToolUse payload for ADO's own create_* tool carries the real session id
    and the new request's id — authoritative, unlike the MCP server's own guess."""
    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    request_id = db.create_decision_request(
        conn, project_id=project["id"], title="Q", question="Q?", origin_session_id="stale-elsewhere",
    )

    result = _post_tool_use(
        env, "mcp__ai-decision-orchestrator__create_decision_request", {"project": "acme"},
        [{"type": "text", "text": json.dumps({"id": request_id})}],
    )

    assert result.returncode == 0 and result.stdout.strip() == "{}"
    assert db.get_decision_request(conn, request_id)["origin_session_id"] == "sess-1"
    assert db.list_sessions(conn)[0]["open_request_count"] == 1


def test_heartbeat_ignores_unparseable_create_tool_responses(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    for response in ("not json", [{"type": "text", "text": "oops"}], {"weird": True}, None):
        result = _post_tool_use(env, "mcp__ai-decision-orchestrator__create_action_request", {}, response)
        assert result.returncode == 0 and result.stdout.strip() == "{}"


def test_heartbeat_task_update_can_rename_and_delete(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    _post_tool_use(env, "TaskCreate", {"subject": "Old", "description": "d"}, {"task": {"id": "1", "subject": "Old"}})

    _post_tool_use(env, "TaskUpdate", {"taskId": "1", "subject": "New"}, {"success": True, "taskId": "1"})
    assert _items()[0]["title"] == "New"

    _post_tool_use(env, "TaskUpdate", {"taskId": "1", "status": "deleted"}, {"success": True, "taskId": "1"})
    assert _items() == []


def test_task_created_hook_and_post_tool_use_do_not_duplicate(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    run_cli("checklist-item-created", env=env, stdin=json.dumps({
        "session_id": "sess-1", "task_id": "1", "task_subject": "Fix bug",
    }))
    _post_tool_use(env, "TaskCreate", {"subject": "Fix bug", "description": "d"}, {"task": {"id": "1", "subject": "Fix bug"}})

    assert len(_items()) == 1


def test_heartbeat_syncs_checklist_from_todo_write(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    _post_tool_use(env, "TodoWrite", {"todos": [
        {"content": "Read code", "status": "completed", "activeForm": "Reading code"},
        {"content": "Write fix", "status": "in_progress", "activeForm": "Writing fix"},
    ]}, {})

    assert [(i["title"], i["status"]) for i in _items()] == [("Read code", "done"), ("Write fix", "in_progress")]


def test_heartbeat_with_malformed_task_payload_still_fails_open(db_path, tmp_path):
    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    for tool_name in ("TaskCreate", "TaskUpdate", "TodoWrite"):
        result = _post_tool_use(env, tool_name, "not-a-dict", ["nope"])
        assert result.returncode == 0 and result.stdout.strip() == "{}"
    assert _items() == []


def _write_transcript(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_session_stop_records_last_done_summary(db_path, tmp_path):
    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        {"type": "ai-title", "aiTitle": "Login bug fix"},
        {"type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": "why does login fail?"}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},
    ])

    result = run_cli("session-stop", env=env, stdin=json.dumps({
        "session_id": "sess-1", "hook_event_name": "Stop", "transcript_path": str(transcript),
        "last_assistant_message": "Fixed the token refresh; tests pass.",
    }))

    assert result.returncode == 0 and result.stdout.strip() == "{}"
    s = db.list_sessions(db.get_connection())[0]
    assert s["status"] == "idle"
    assert s["last_summary"] == "Fixed the token refresh; tests pass."
    assert s["title"] == "Login bug fix"
    assert s["last_prompt"] == "why does login fail?"


def test_session_stop_does_not_show_a_recap_older_than_the_latest_prompt_as_new(db_path, tmp_path):
    """Live symptom: an hours-old recap re-saved at every Stop with last_summary_at =
    now, so the card said "Last done 2m ago" over work from before the latest ask."""
    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        {"type": "system", "subtype": "away_summary", "content": "Old recap.", "timestamp": "2026-10-01T09:00:00.000Z"},
        {"type": "user", "origin": {"kind": "human"}, "message": {"content": "now fix the webhook"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Fixed it."}]}},
    ])

    run_cli("session-stop", env=env, stdin=json.dumps({
        "session_id": "sess-1", "hook_event_name": "Stop", "transcript_path": str(transcript),
        "last_assistant_message": "Fixed the webhook; tests pass.",
    }))

    s = db.list_sessions(db.get_connection())[0]
    assert s["last_summary"] == "Fixed the webhook; tests pass."
    assert s["last_summary_at"] > "2026-10-02"


def test_session_stop_stamps_a_current_recap_with_its_own_time(db_path, tmp_path):
    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        {"type": "user", "origin": {"kind": "human"}, "message": {"content": "fix it"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Fixed."}]}},
        {"type": "system", "subtype": "away_summary", "content": "Fixed it all.", "timestamp": "2026-10-07T12:06:05.571Z"},
    ])

    run_cli("session-stop", env=env, stdin=json.dumps({
        "session_id": "sess-1", "hook_event_name": "Stop", "transcript_path": str(transcript),
    }))

    s = db.list_sessions(db.get_connection())[0]
    assert s["last_summary"] == "Fixed it all."
    assert s["last_summary_at"].startswith("2026-10-07T12:06:05")


def test_session_stop_prefers_away_summary_over_last_assistant_message(db_path, tmp_path):
    """away_summary (Claude Code's own built-in recap) is a real synthesized
    catch-up — strictly better than either the hook payload's raw last message
    or the transcript's raw last assistant text, so it wins when present."""
    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Raw reply."}]}},
        {"type": "system", "subtype": "away_summary", "content": "Synthesized catch-up. (disable recaps in /config)"},
    ])

    run_cli("session-stop", env=env, stdin=json.dumps({
        "session_id": "sess-1", "hook_event_name": "Stop", "transcript_path": str(transcript),
        "last_assistant_message": "Fixed the token refresh; tests pass.",
    }))

    s = db.list_sessions(db.get_connection())[0]
    assert s["last_summary"] == "Synthesized catch-up."


def test_session_stop_falls_back_to_transcript_for_last_message(db_path, tmp_path):
    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Earlier."}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Final answer."},
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash"}]}},
    ])

    run_cli("session-stop", env=env, stdin=json.dumps({"session_id": "sess-1", "transcript_path": str(transcript)}))

    assert db.list_sessions(db.get_connection())[0]["last_summary"] == "Final answer."


def test_backfill_summaries_prefers_away_summary(db_path, tmp_path):
    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path, "sess-1")
    projects = tmp_path / "projects"
    (projects / "-some-repo").mkdir(parents=True)
    transcript = projects / "-some-repo" / "sess-1.jsonl"
    _write_transcript(transcript, [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Raw reply."}]}},
        {"type": "system", "subtype": "away_summary", "content": "Synthesized catch-up."},
    ])

    run_cli("backfill-summaries", "--projects-dir", str(projects), env=env)

    assert db.list_sessions(db.get_connection())[0]["last_summary"] == "Synthesized catch-up."


def test_backfill_summaries_reads_transcripts_by_session_id(db_path, tmp_path):
    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path, "sess-1")
    _start(env, tmp_path, "sess-no-transcript")
    projects = tmp_path / "projects"
    (projects / "-some-repo").mkdir(parents=True)
    transcript = projects / "-some-repo" / "sess-1.jsonl"
    _write_transcript(transcript, [
        {"type": "ai-title", "aiTitle": "Old work"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Wrapped up the migration."}]}},
    ])
    os.utime(transcript, (1_700_000_000, 1_700_000_000))

    result = run_cli("backfill-summaries", "--projects-dir", str(projects), env=env)

    assert result.returncode == 0, result.stderr
    assert "1" in result.stdout
    by_id = {s["claude_session_id"]: s for s in db.list_sessions(db.get_connection())}
    assert by_id["sess-1"]["last_summary"] == "Wrapped up the migration."
    assert by_id["sess-1"]["title"] == "Old work"
    assert by_id["sess-1"]["last_summary_at"].startswith("2023-11-14T22:13:20")
    assert by_id["sess-no-transcript"]["last_summary"] is None


def test_session_stop_with_missing_transcript_still_marks_idle(db_path, tmp_path):
    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)

    result = run_cli("session-stop", env=env, stdin=json.dumps({
        "session_id": "sess-1", "transcript_path": str(tmp_path / "missing.jsonl"),
    }))

    assert result.returncode == 0 and result.stdout.strip() == "{}"
    assert db.list_sessions(db.get_connection())[0]["status"] == "idle"


def test_a_usage_limit_stop_keeps_claude_codes_message_and_its_reset_time(db_path, tmp_path):
    from apo import db

    env = dict(os.environ, APO_DB=str(db_path))
    _start(env, tmp_path)
    _hook(env, "session-prompt", prompt="go")

    _hook(env, "session-stop-failure", error="rate_limit",
          last_assistant_message="You've hit your limit · resets 4pm (Europe/Berlin)")

    row = db.get_session(db.get_connection(), "sess-1")
    assert "resets 4pm" in row["last_error_detail"]
    assert row["limit_resets_at"]
