"""Which folder a session or a filing belongs to.

Live incident: a session started in the home directory auto-registered a project
named after the user's home folder at ~ itself. Registered folders match their
subfolders, so every unregistered project under ~ silently became part of it.
"""
import sqlite3
from pathlib import Path

import pytest

from apo import db


def _git_repo(path):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def test_an_empty_dot_git_folder_is_not_a_repository(db_path, tmp_path):
    (tmp_path / "stray" / ".git").mkdir(parents=True)
    (tmp_path / "stray" / "proj").mkdir()

    assert db.git_root(str(tmp_path / "stray" / "proj")) is None


def test_the_home_directory_is_never_registered_as_a_project(db_path, tmp_path, monkeypatch):
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    conn = db.get_connection()

    db.register_session(conn, "in-home", str(home))
    db.register_session(conn, "above-home", str(home.parent))

    for s in db.list_sessions(conn):
        assert s["project_id"] is None, s["claude_session_id"]
    assert conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 0


def test_a_session_in_a_subfolder_registers_the_git_repository_root(db_path, tmp_path):
    repo = _git_repo(tmp_path / "webapp")
    (repo / "src" / "api").mkdir(parents=True)
    conn = db.get_connection()

    db.register_session(conn, "sess-1", str(repo / "src" / "api"))

    s = db.list_sessions(conn)[0]
    assert s["project_name"] == "webapp"
    assert db.get_repository(conn, s["repository_id"])["local_path"] == str(repo)


def test_a_worktree_belongs_to_its_main_repository(db_path, tmp_path):
    repo = _git_repo(tmp_path / "webapp")
    worktree = repo / ".claude" / "worktrees" / "feature-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/feature-x\n")
    conn = db.get_connection()

    db.register_session(conn, "sess-1", str(worktree))

    s = db.list_sessions(conn)[0]
    assert s["project_name"] == "webapp"
    assert db.get_repository(conn, s["repository_id"])["local_path"] == str(repo)


def test_a_registered_folder_does_not_swallow_a_separate_git_repository_inside_it(db_path, tmp_path):
    container = tmp_path / "projects"
    container.mkdir()
    inner = _git_repo(container / "newthing")
    conn = db.get_connection()
    outer = db.get_or_create_project(conn, "projects")
    db.create_repository(conn, outer["id"], "projects", str(container))

    assert db.find_repository_by_path(conn, str(inner)) is None
    db.register_session(conn, "sess-1", str(inner))
    assert db.list_sessions(conn)[0]["project_name"] == "newthing"


def test_a_registered_folder_still_matches_its_own_subfolders(db_path, tmp_path):
    repo = _git_repo(tmp_path / "webapp")
    (repo / "src").mkdir()
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "webapp")
    registered = db.create_repository(conn, project["id"], "webapp", str(repo))

    assert db.find_repository_by_path(conn, str(repo / "src"))["id"] == registered["id"]


def test_a_session_resumed_from_home_keeps_its_project(db_path, tmp_path, monkeypatch):
    """Live: after a reboot, sessions were resumed from ~ and lost their projects.
    ~ says nothing about which project a session is for; its history does."""
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    repo = _git_repo(home / "projects" / "webapp")
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(repo))

    db.register_session(conn, "sess-1", str(home))

    s = db.list_sessions(conn)[0]
    assert s["project_name"] == "webapp"
    assert s["cwd"] == str(home)


def test_reconcile_follows_a_session_into_another_folder(db_path, tmp_path):
    """A session's folder can change mid-session (cd, entering a worktree); hooks
    only report it at SessionStart, Claude Code's live view always has it."""
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    conn = db.get_connection()
    db.register_session(conn, "sess-1", str(first))
    db.session_heartbeat(conn, "sess-1")

    db.reconcile_live_sessions(conn, [{"pid": 3, "sessionId": "sess-1", "cwd": str(second), "status": "busy"}])

    s = db.list_sessions(conn)[0]
    assert s["project_name"] == "second"
    assert s["cwd"] == str(second)


def test_upgrading_drops_a_home_directory_registration_with_no_decisions(db_path, tmp_path, monkeypatch):
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    conn = db.get_connection()
    stray = db.get_or_create_project(conn, "me")
    stray_repo = db.create_repository(conn, stray["id"], "me", str(home))
    conn.execute(
        "INSERT INTO sessions (claude_session_id, project_id, repository_id, cwd, status, started_at, last_seen_at) "
        "VALUES ('s', ?, ?, ?, 'running', '2026-10-09T00:00:00+00:00', '2026-10-09T00:00:00+00:00')",
        (stray["id"], stray_repo["id"], str(home)),
    )
    kept = db.get_or_create_project(conn, "kept")
    kept_repo = db.create_repository(conn, kept["id"], "kept", str(home.parent))
    db.create_decision_request(conn, project_id=kept["id"], repository_id=kept_repo["id"], title="Q", question="Q?")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()

    conn = db.get_connection()

    names = {r["name"] for r in conn.execute("SELECT name FROM projects")}
    assert "me" not in names
    assert "kept" in names  # has a decision on record: left for a human to sort out
    session = conn.execute("SELECT project_id, repository_id FROM sessions WHERE claude_session_id = 's'").fetchone()
    assert session["project_id"] is None and session["repository_id"] is None


# --- A folder that was renamed or moved ---
#
# Live incident: ~/projects/passiveIncome2 was renamed to ~/projects/basari. Its row
# kept the old path, a session in the new folder registered it a second time, and
# answering a decision recreated the old folder to write into.


def _renamed(tmp_path, project_name="basari"):
    """A project whose folder held a decision and was then renamed on disk."""
    old = tmp_path / "x" / "passiveIncome2"
    old.mkdir(parents=True)
    conn = db.get_connection()
    project = db.get_or_create_project(conn, project_name)
    repo = db.create_repository(conn, project["id"], "passiveIncome2", str(old))
    decision_id = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Pick a host", question="Q?",
    )
    new = tmp_path / "x" / "basari"
    old.rename(new)
    return conn, project, repo, decision_id, old, new


def _index(folder):
    path = folder / ".claude" / "decisions" / "INDEX.md"
    return path.read_text() if path.exists() else ""


def test_relocating_a_folder_updates_its_path_and_name_in_place(db_path, tmp_path):
    conn, project, repo, decision_id, old, new = _renamed(tmp_path)

    result = db.relocate_repository(conn, repo["id"], str(new))

    moved = db.get_repository(conn, repo["id"])
    assert moved["local_path"] == str(new) and moved["name"] == "basari"
    assert result["repository"]["id"] == repo["id"] and result["old_path"] == str(old)
    assert db.get_decision_request(conn, decision_id)["repository_id"] == repo["id"]
    assert f"| OPEN | {decision_id:04d} |" in _index(new)
    assert not old.exists()


def test_relocating_folds_in_the_copy_a_session_registered_at_the_new_path(db_path, tmp_path):
    conn, project, repo, decision_id, old, new = _renamed(tmp_path)
    db.register_session(conn, "in-new-folder", str(new))
    twin = db.find_repository_by_path(conn, str(new))
    assert twin["id"] != repo["id"] and twin["project_id"] == project["id"]
    later = db.create_task(conn, project_id=project["id"], title="Filed from the new folder", repository_id=twin["id"])

    result = db.relocate_repository(conn, repo["id"], str(new))

    assert db.get_repository(conn, twin["id"]) is None
    rows = conn.execute("SELECT id, name, local_path FROM repositories WHERE project_id = ?", (project["id"],)).fetchall()
    assert [(r["id"], r["name"], r["local_path"]) for r in rows] == [(repo["id"], "basari", str(new))]
    assert db.get_decision_request(conn, later)["repository_id"] == repo["id"]
    session = conn.execute("SELECT repository_id FROM sessions WHERE claude_session_id = 'in-new-folder'").fetchone()
    assert session["repository_id"] == repo["id"]
    assert result["folded"] == 1 and result["decisions"] == 1 and result["sessions"] == 1
    index = _index(new)
    assert f"{decision_id:04d}" in index and f"{later:04d}" in index
    assert not old.exists()


def test_after_relocating_answers_and_moves_land_in_the_new_folder(db_path, tmp_path):
    conn, project, repo, decision_id, old, new = _renamed(tmp_path)
    db.register_session(conn, "in-new-folder", str(new))
    db.relocate_repository(conn, repo["id"], str(new))

    db.answer_decision_request(conn, decision_id, "Use Fly.")

    assert f"| ANSWERED | {decision_id:04d} |" in _index(new)
    assert "Use Fly." in (new / ".claude" / "decisions" / f"{decision_id:04d}-pick-a-host.md").read_text()
    assert not old.exists()


def test_the_renamed_folder_repro_writes_nothing_into_the_old_path(db_path, tmp_path):
    """The task's steps: register a, file there, mv a b, start a session in b (which
    registers b a second time), answer. Before relocating, nothing recreates a; after
    it, the answer is written under b."""
    conn, project, repo, decision_id, old, new = _renamed(tmp_path)
    second = db.create_decision_request(
        conn, project_id=project["id"], repository_id=repo["id"], title="Pick a region", question="Q?",
    )
    db.register_session(conn, "in-new-folder", str(new))
    twin = db.find_repository_by_path(conn, str(new))

    db.answer_decision_request(conn, decision_id, "Use Fly.")
    db.move_item(conn, second, twin["id"])
    assert not old.exists()
    assert any(str(old) in e["message"] for e in db.recent_sync_errors(conn, limit=50))

    db.relocate_repository(conn, repo["id"], str(new))

    assert not old.exists()
    assert f"| ANSWERED | {decision_id:04d} |" in _index(new)
    assert f"| OPEN | {second:04d} |" in _index(new)
    assert "Use Fly." in (new / ".claude" / "decisions" / f"{decision_id:04d}-pick-a-host.md").read_text()
    assert db.recent_sync_errors(conn, unresolved_only=True) == []


def test_relocating_refuses_an_unknown_folder_a_missing_path_and_another_projects_folder(db_path, tmp_path):
    conn, project, repo, decision_id, old, new = _renamed(tmp_path)
    other = db.get_or_create_project(conn, "globex")
    theirs = tmp_path / "x" / "globex"
    theirs.mkdir()
    db.create_repository(conn, other["id"], "globex", str(theirs))

    for repository_id, path, says in ((9999, str(new), "No folder"),
                                      (repo["id"], str(tmp_path / "x" / "nowhere"), "isn't a folder"),
                                      (repo["id"], str(theirs), "globex"),
                                      (repo["id"], str(Path.home()), "home folder"),
                                      (repo["id"], "", "full new path"),
                                      (repo["id"], "x/basari", "full new path")):
        try:
            db.relocate_repository(conn, repository_id, path)
        except ValueError as exc:
            assert says in str(exc)
        else:
            raise AssertionError(f"expected ValueError for {path}")

    assert db.get_repository(conn, repo["id"])["local_path"] == str(old)
    assert not old.exists()


def test_a_failed_relocate_changes_nothing(db_path, tmp_path):
    conn, project, repo, decision_id, old, new = _renamed(tmp_path)
    db.register_session(conn, "in-new-folder", str(new))
    twin = db.find_repository_by_path(conn, str(new))
    conn.execute("CREATE TEMP TRIGGER fail BEFORE UPDATE OF local_path ON repositories "
                 "BEGIN SELECT RAISE(ABORT, 'disk on fire'); END")

    with pytest.raises(sqlite3.Error):
        db.relocate_repository(conn, repo["id"], str(new))
    conn.commit()

    assert db.get_repository(conn, twin["id"]) is not None
    assert db.get_repository(conn, repo["id"])["local_path"] == str(old)
    session = conn.execute("SELECT repository_id FROM sessions WHERE claude_session_id = 'in-new-folder'").fetchone()
    assert session["repository_id"] == twin["id"]


def test_missing_folders_are_listed(db_path, tmp_path):
    conn, project, repo, decision_id, old, new = _renamed(tmp_path)
    here = tmp_path / "x" / "here"
    here.mkdir()
    other = db.get_or_create_project(conn, "globex")
    db.create_repository(conn, other["id"], "here", str(here))

    missing = db.missing_folders(conn)

    assert [(m["id"], m["local_path"], m["project_name"]) for m in missing] == [(repo["id"], str(old), "basari")]
