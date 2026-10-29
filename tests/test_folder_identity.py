"""Which folder a session or a filing belongs to.

Live incident: a session started in the home directory auto-registered a project
named after the user's home folder at ~ itself. Registered folders match their
subfolders, so every unregistered project under ~ silently became part of it.
"""
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
