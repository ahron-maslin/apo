"""The in-repo markdown projection.

Every test here goes through db.py rather than calling repo_writer directly wherever
it can, because the whole point of this layer is that it fires automatically on each
state change — a test that only called repo_writer.sync_* would pass even if nothing
were wired up.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK_SCRIPT = str(Path(__file__).resolve().parent.parent / "src" / "apo" / "resources" / "hooks" / "session-decisions-nudge.sh")


@pytest.fixture
def repo_project(db_path, tmp_path):
    """A project with one registered repository at tmp_path, plus the modules under
    test imported *after* db_path blanked the environment (see conftest.db_path)."""
    from apo import db
    from apo import repo_writer

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", str(tmp_path))
    return conn, db, repo_writer, project, repo


def read_index(tmp_path):
    return (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()


def test_open_decision_lands_in_repo_as_open(repo_project, tmp_path):
    conn, db, _, project, repo = repo_project
    decision_id = db.create_decision_request(
        conn,
        project_id=project["id"],
        repository_id=repo["id"],
        title="Which auth provider?",
        question="Auth0, Clerk, or roll our own?",
        context="Signup flow is otherwise done.",
    )

    index = read_index(tmp_path)
    assert f"| OPEN | {decision_id:04d} | Which auth provider? |" in index
    assert "answered=0 open=1 done=0" in index

    body = (tmp_path / ".claude" / "decisions" / f"{decision_id:04d}-which-auth-provider.md").read_text()
    assert "Auth0, Clerk, or roll our own?" in body
    assert "Signup flow is otherwise done." in body
    assert "**Status:** OPEN" in body


def test_answer_writes_answer_into_repo_and_flips_index_to_answered(repo_project, tmp_path):
    conn, db, _, project, repo = repo_project
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"],
        title="Which auth provider?", question="Auth0, Clerk, or roll our own?",
        resume_plan={"objective": "Finish the signup flow", "remaining_steps": ["Wire the callback"]},
    )

    db.answer_decision_request(conn, decision_id, "Use Clerk — we already pay for it.", source="linear")

    index = read_index(tmp_path)
    assert f"| ANSWERED | {decision_id:04d} |" in index
    assert "answered=1 open=0 done=0" in index

    body = (tmp_path / ".claude" / "decisions" / f"{decision_id:04d}-which-auth-provider.md").read_text()
    assert "Use Clerk — we already pay for it." in body
    assert "**Status:** ANSWERED" in body
    assert "via linear" in body
    assert "Finish the signup flow" in body
    assert "Wire the callback" in body


def test_resolving_marks_done_and_stops_showing_as_actionable(repo_project, tmp_path):
    conn, db, _, project, repo = repo_project
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?",
    )
    db.answer_decision_request(conn, decision_id, "An answer")
    db.resolve_decision_request(conn, decision_id)

    index = read_index(tmp_path)
    assert f"| DONE | {decision_id:04d} |" in index
    assert "answered=0 open=0 done=1" in index
    assert "**Status:** DONE" in (tmp_path / ".claude" / "decisions" / f"{decision_id:04d}-q.md").read_text()


def test_index_puts_actionable_rows_first(repo_project, tmp_path):
    """The whole cheap-query contract: an agent that reads only the head of INDEX.md
    must see every ANSWERED row, however much resolved history has piled up below."""
    conn, db, _, project, repo = repo_project
    for i in range(3):
        done_id = db.create_decision_request(
            conn, project_id=project["id"], repository_id=repo["id"],
            title=f"Old thing {i}", question="Q?",
        )
        db.answer_decision_request(conn, done_id, "yes")
        db.resolve_decision_request(conn, done_id)
    open_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Still asking", question="Q?",
    )
    answered_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Ready to work", question="Q?",
    )
    db.answer_decision_request(conn, answered_id, "Go")

    index = read_index(tmp_path)
    assert index.index(f"| ANSWERED | {answered_id:04d}") < index.index(f"| OPEN | {open_id:04d}")
    assert index.index(f"| OPEN | {open_id:04d}") < index.index("| DONE |")
    assert "answered=1 open=1 done=3" in index


def test_decision_without_repository_id_uses_the_projects_only_repo(repo_project, tmp_path):
    """Decisions filed before a repo was registered (repository_id NULL) still project
    into the repo, as long as the project has exactly one — same fallback rule as
    ensure_repository_for_project, so old rows aren't silently invisible."""
    conn, db, _, project, _ = repo_project
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], title="Orphan", question="Q?",
    )
    db.answer_decision_request(conn, decision_id, "Answered anyway")

    assert f"| ANSWERED | {decision_id:04d} |" in read_index(tmp_path)


def test_project_with_no_repository_writes_nothing_and_does_not_raise(db_path, tmp_path):
    from apo import db

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "no-repo")
    decision_id = db.create_decision_request(conn, project_id=project["id"], title="Q", question="Q?")
    assert db.answer_decision_request(conn, decision_id, "An answer") is True
    assert not (tmp_path / ".claude").exists()


def test_unwritable_repo_records_a_sync_error_instead_of_breaking_the_answer(db_path, tmp_path):
    """A repo that moved or a permissions problem must not take down the answer path —
    the DB write is what matters, the markdown is a projection of it. Losing the
    projection silently would be worse than noisy, hence the sync_errors row."""
    from apo import db

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this is a file, so mkdir underneath it fails")

    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", str(blocker))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?",
    )

    assert db.answer_decision_request(conn, decision_id, "An answer") is True
    assert db.get_decision_request(conn, decision_id)["status"] == "answered"
    errors = db.recent_sync_errors(conn)
    assert any(e["source"] == "repo_writer" and e["decision_request_id"] == decision_id for e in errors)


def test_rewrite_removes_a_stale_decision_file(repo_project, tmp_path):
    """Nothing renames a decision today, but a stale NNNN-*.md would read as a live
    decision to an agent and there'd be no way to tell which file was authoritative."""
    conn, db, repo_writer, project, repo = repo_project
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Real title", question="Q?",
    )
    stale = tmp_path / ".claude" / "decisions" / f"{decision_id:04d}-an-old-title.md"
    stale.write_text("# stale")

    repo_writer.sync_repository(conn, dict(repo))

    assert not stale.exists()
    assert (tmp_path / ".claude" / "decisions" / f"{decision_id:04d}-real-title.md").exists()


def test_two_projects_sharing_a_folder_never_delete_each_others_files(db_path, tmp_path):
    """Live incident: Umbrella and Hooli were both registered at one path, and
    syncing Hooli pruned Umbrella's 0008/0009 as "stale". The projection is per
    folder: everything filed against that folder belongs in it."""
    from apo import db

    conn = db.get_connection()
    first = db.get_or_create_project(conn, "Umbrella")
    second = db.get_or_create_project(conn, "Hooli")
    first_repo = db.create_repository(conn, first["id"], "ts", str(tmp_path))
    second_repo = db.create_repository(conn, second["id"], "rs", str(tmp_path))
    early = db.create_decision_request(
        conn, project_id=first["id"], repository_id=first_repo["id"], title="Seed pack", question="Q?",
    )
    late = db.create_decision_request(
        conn, project_id=second["id"], repository_id=second_repo["id"], title="Difficulty ramp", question="Q?",
    )
    db.answer_decision_request(conn, late, "go")

    files = {p.name for p in (tmp_path / ".claude" / "decisions").iterdir()}
    assert f"{early:04d}-seed-pack.md" in files
    assert f"{late:04d}-difficulty-ramp.md" in files
    index = read_index(tmp_path)
    assert f"| OPEN | {early:04d} |" in index
    assert f"| ANSWERED | {late:04d} |" in index


def test_rewrite_leaves_unrelated_files_alone(repo_project, tmp_path):
    conn, db, repo_writer, project, repo = repo_project
    db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?",
    )
    keep = tmp_path / ".claude" / "decisions" / "NOTES.md"
    keep.write_text("hand-written notes")

    repo_writer.sync_repository(conn, dict(repo))

    assert keep.read_text() == "hand-written notes"


def test_answering_one_decision_does_not_rerender_others(repo_project, tmp_path, monkeypatch):
    """The perf-critical property: a state change on one decision must not pay for
    every other decision ever filed in the project."""
    conn, db, repo_writer, project, repo = repo_project
    ids = [
        db.create_decision_request(
            conn, project_id=project["id"], repository_id=repo["id"], title=f"Q{i}", question="Q?",
        )
        for i in range(4)
    ]

    rendered = []
    original = repo_writer._render_decision

    def spy(decision, project_name):
        rendered.append(decision["id"])
        return original(decision, project_name)

    monkeypatch.setattr(repo_writer, "_render_decision", spy)

    db.answer_decision_request(conn, ids[2], "Answering only this one")

    assert rendered == [ids[2]]


def test_repository_resolution_is_memoized_per_sync(repo_project, tmp_path, monkeypatch):
    conn, db, repo_writer, project, repo = repo_project
    ids = [
        db.create_decision_request(
            conn, project_id=project["id"], repository_id=repo["id"], title=f"Q{i}", question="Q?",
        )
        for i in range(5)
    ]

    calls = []
    original = repo_writer._resolve_repository

    def spy(conn_, repository_id, project_id):
        calls.append(repository_id)
        return original(conn_, repository_id, project_id)

    monkeypatch.setattr(repo_writer, "_resolve_repository", spy)

    db.answer_decision_request(conn, ids[0], "Go")

    # One resolution for sync_decision's own target repo, one more for the single
    # distinct repository_id shared by all five decisions — never once per decision.
    assert len(calls) <= 2


def test_action_request_renders_kind_metadata_line(repo_project, tmp_path):
    conn, db, _, project, repo = repo_project
    action_id = db.create_action_request(
        conn, project_id=project["id"], repository_id=repo["id"],
        title="Grant S3 access", question="Please grant write access.",
    )

    body = (tmp_path / ".claude" / "decisions" / f"{action_id:04d}-grant-s3-access.md").read_text()
    assert "**Kind:** Action" in body


def test_decision_request_renders_kind_metadata_line(repo_project, tmp_path):
    conn, db, _, project, repo = repo_project
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?",
    )

    body = (tmp_path / ".claude" / "decisions" / f"{decision_id:04d}-q.md").read_text()
    assert "**Kind:** Decision" in body


def test_index_prefixes_action_titles_with_bracket_tag_not_a_new_column(repo_project, tmp_path):
    """Critical constraint: INDEX.md's column count must never change —
    hooks/session-decisions-nudge.sh parses it positionally. kind is signaled by
    prefixing the title cell's text, not by adding a column."""
    conn, db, _, project, repo = repo_project
    action_id = db.create_action_request(
        conn, project_id=project["id"], repository_id=repo["id"],
        title="Grant S3 access", question="Please grant write access.",
    )
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Pick a provider", question="Q?",
    )

    index = read_index(tmp_path)
    assert f"| {action_id:04d} | [ACTION] Grant S3 access |" in index
    assert f"| {decision_id:04d} | Pick a provider |" in index
    for line in index.splitlines():
        if line.startswith("|"):
            assert line.count("|") == 5  # unchanged: | status | id | title | file |


def test_answered_action_footer_says_confirm_done_not_do_the_work(repo_project, tmp_path):
    conn, db, _, project, repo = repo_project
    action_id = db.create_action_request(
        conn, project_id=project["id"], repository_id=repo["id"],
        title="Grant S3 access", question="Please grant write access.",
    )
    db.answer_decision_request(conn, action_id, "Granted via the AWS console.")

    body = (tmp_path / ".claude" / "decisions" / f"{action_id:04d}-grant-s3-access.md").read_text()
    assert "Confirm the action above is actually done" in body
    assert "Do the work above" not in body


def test_answered_decision_footer_still_says_do_the_work(repo_project, tmp_path):
    conn, db, _, project, repo = repo_project
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?",
    )
    db.answer_decision_request(conn, decision_id, "An answer")

    body = (tmp_path / ".claude" / "decisions" / f"{decision_id:04d}-q.md").read_text()
    assert "Do the work above" in body
    assert "Confirm the action above" not in body


def test_nudge_hook_still_extracts_correct_id_and_title_with_bracket_prefix(repo_project, tmp_path):
    """Regression coverage for the hook's positional awk parsing — nothing exercised
    this bash script at all before kind support was added."""
    conn, db, _, project, repo = repo_project
    action_id = db.create_action_request(
        conn, project_id=project["id"], repository_id=repo["id"],
        title="Grant S3 access", question="Please grant write access.",
    )
    db.answer_decision_request(conn, action_id, "Will grant it.")

    result = subprocess.run(
        [HOOK_SCRIPT], cwd=str(tmp_path), input="{}", capture_output=True, text=True,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert f"#{action_id:04d} [ACTION] Grant S3 access" in payload["systemMessage"]


def test_nudge_hook_mentions_tasks_ready_for_an_agent(repo_project, tmp_path):
    conn, db, _, project, repo = repo_project
    db.create_task(conn, project_id=project["id"], repository_id=repo["id"], title="Add rate limiting")
    answered = db.create_decision_request(conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?")
    db.answer_decision_request(conn, answered, "yes")

    result = subprocess.run([HOOK_SCRIPT], cwd=str(tmp_path), input="{}", capture_output=True, text=True)

    message = json.loads(result.stdout)["systemMessage"]
    assert "1 answered decision and 1 task assigned to an agent" in message
    assert "Add rate limiting (READY)" in message


def test_nudge_hook_does_not_name_a_specific_person(repo_project, tmp_path):
    """It ships to teammates via `ado init`."""
    conn, db, _, project, repo = repo_project
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?",
    )
    db.answer_decision_request(conn, decision_id, "yes")

    result = subprocess.run([HOOK_SCRIPT], cwd=str(tmp_path), input="{}", capture_output=True, text=True)

    message = json.loads(result.stdout)["systemMessage"]
    assert "The user can run /decisions" in message


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_attachments_are_copied_into_the_repo_and_linked_from_markdown(repo_project, tmp_path):
    conn, db, _, project, repo = repo_project
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Which auth provider?", question="Q?",
    )
    data = PNG_MAGIC + b"fake-png-bytes"
    saved = db.save_attachment(conn, decision_id, "screenshot.png", "image/png", data)

    db.answer_decision_request(conn, decision_id, "Use Clerk.")

    copied = (
        tmp_path / ".claude" / "decisions" / "attachments"
        / f"{decision_id:04d}" / saved["stored_name"]
    )
    assert copied.read_bytes() == data

    body = (tmp_path / ".claude" / "decisions" / f"{decision_id:04d}-which-auth-provider.md").read_text()
    assert "## Screenshots" in body
    assert f"![screenshot.png](attachments/{decision_id:04d}/{saved['stored_name']})" in body


def test_attachment_copy_failure_records_sync_error_and_does_not_raise(repo_project, tmp_path, monkeypatch):
    conn, db, repo_writer, project, repo = repo_project
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Q", question="Q?",
    )
    saved = db.save_attachment(conn, decision_id, "screenshot.png", "image/png", PNG_MAGIC + b"data")

    from apo import config
    source_path = config.get_attachments_dir() / str(decision_id) / saved["stored_name"]
    source_path.unlink()

    assert db.answer_decision_request(conn, decision_id, "An answer") is True
    errors = db.recent_sync_errors(conn)
    assert any(e["source"] == "repo_writer" and e["decision_request_id"] == decision_id for e in errors)


def test_successful_write_resolves_earlier_repo_writer_errors_for_the_project(db_path, tmp_path):
    """The projection is rewritten whole on every write, so one success means anything
    that failed to land earlier for that repository has now been written."""
    from datetime import datetime, timedelta, timezone

    from apo import db

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    repo = db.create_repository(conn, project["id"], "backend", str(blocker))
    first = db.create_decision_request(conn, project_id=project["id"], repository_id=repo["id"], title="A", question="Q?")
    assert db.recent_sync_errors(conn, unresolved_only=True)
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn.execute("UPDATE sync_errors SET occurred_at = ?", (old,))
    good = tmp_path / "checkout"
    good.mkdir()
    conn.execute("UPDATE repositories SET local_path = ? WHERE id = ?", (str(good), repo["id"]))
    conn.commit()

    db.create_decision_request(conn, project_id=project["id"], repository_id=repo["id"], title="B", question="Q?")

    assert db.recent_sync_errors(conn, unresolved_only=True) == []
    assert f"| OPEN | {first:04d} |" in (good / ".claude" / "decisions" / "INDEX.md").read_text()


def test_missing_decision_file_self_heals_without_touching_others(repo_project, tmp_path):
    conn, db, repo_writer, project, repo = repo_project
    ids = [
        db.create_decision_request(
            conn, project_id=project["id"], repository_id=repo["id"], title=f"Q{i}", question="Q?",
        )
        for i in range(3)
    ]
    decisions_dir = tmp_path / ".claude" / "decisions"
    missing_path = decisions_dir / f"{ids[1]:04d}-q1.md"
    missing_path.unlink()

    other_path = decisions_dir / f"{ids[0]:04d}-q0.md"
    old_time = other_path.stat().st_mtime - 1000
    os.utime(other_path, (old_time, old_time))

    db.answer_decision_request(conn, ids[2], "Answering the third one")

    assert missing_path.exists()
    assert "**Status:** OPEN" in missing_path.read_text()
    assert other_path.stat().st_mtime == old_time
