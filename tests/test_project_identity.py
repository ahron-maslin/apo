"""One project per folder, whatever the agent calls it.

Live DB had both failure modes: "Widget Exporter" (an agent's free-text name)
and "widget-exporter" (a session's folder name) as two projects for one path;
and Hooli registered as a second project at Umbrella's path after a rename.
"""
from apo import db


def test_name_variants_resolve_to_the_same_project(db_path):
    conn = db.get_connection()
    original = db.get_or_create_project(conn, "Widget Exporter")

    for variant in ("widget-exporter", "widget_exporter", "WIDGET EXPORTER"):
        assert db.get_or_create_project(conn, variant)["id"] == original["id"]
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_an_alias_resolves_to_its_project(db_path):
    conn = db.get_connection()
    reflex = db.get_or_create_project(conn, "Hooli")
    db.add_project_alias(conn, "Umbrella", reflex["id"])

    assert db.get_or_create_project(conn, "umbrella")["id"] == reflex["id"]


def test_filing_from_a_registered_folder_under_an_unknown_name_joins_that_folders_project(db_path, tmp_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "Umbrella")
    repo = db.create_repository(conn, project["id"], "Umbrella", str(tmp_path))

    resolved, repository_id = db.resolve_project_for_filing(conn, "Hooli", str(tmp_path / "src"))

    assert resolved["id"] == project["id"]
    assert repository_id == repo["id"]
    assert db.get_or_create_project(conn, "Hooli")["id"] == project["id"]  # remembered as an alias
    assert conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 1


def test_filing_under_a_known_name_from_another_folder_trusts_the_name(db_path, tmp_path):
    """A session in one repo filing about another known project said which one."""
    conn = db.get_connection()
    here = db.get_or_create_project(conn, "dotfiles")
    db.create_repository(conn, here["id"], "dotfiles", str(tmp_path / "dotfiles"))
    there = db.get_or_create_project(conn, "Globex")
    there_repo = db.create_repository(conn, there["id"], "globex", str(tmp_path / "globex"))

    resolved, repository_id = db.resolve_project_for_filing(conn, "Globex", str(tmp_path / "dotfiles"))

    assert resolved["id"] == there["id"]
    assert repository_id == there_repo["id"]


def test_filing_a_new_project_from_an_unregistered_folder_registers_it(db_path, tmp_path):
    conn = db.get_connection()
    resolved, repository_id = db.resolve_project_for_filing(conn, "Fresh", str(tmp_path / "fresh"))

    assert resolved["name"] == "Fresh"
    assert db.get_repository(conn, repository_id)["local_path"] == str(tmp_path / "fresh")


def test_a_later_filing_from_the_same_folder_still_gets_its_repository(db_path, tmp_path):
    """12 of 21 live decisions had no repository: the old helper only registered one
    when the project had none, and returned nothing every time after that."""
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    first = db.ensure_repository_for_project(conn, project["id"], str(tmp_path))

    again = db.ensure_repository_for_project(conn, project["id"], str(tmp_path / "sub" / "dir"))

    assert first is not None and again == first


def test_ensure_repository_never_registers_a_folder_that_belongs_to_another_project(db_path, tmp_path):
    conn = db.get_connection()
    owner = db.get_or_create_project(conn, "owner")
    db.create_repository(conn, owner["id"], "owner", str(tmp_path))
    other = db.get_or_create_project(conn, "other")

    assert db.ensure_repository_for_project(conn, other["id"], str(tmp_path)) is None
    assert conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 1


def test_merge_projects_moves_everything_and_keeps_the_old_name_as_an_alias(db_path, tmp_path):
    conn = db.get_connection()
    old = db.get_or_create_project(conn, "Umbrella")
    new = db.get_or_create_project(conn, "Hooli")
    old_repo = db.create_repository(conn, old["id"], "ts", str(tmp_path))
    new_repo = db.create_repository(conn, new["id"], "rs", str(tmp_path))
    old_decision = db.create_decision_request(
        conn, project_id=old["id"], repository_id=old_repo["id"], title="Old one", question="Q?",
    )
    new_decision = db.create_decision_request(
        conn, project_id=new["id"], repository_id=new_repo["id"], title="New one", question="Q?",
    )
    db.register_session(conn, "sess-1", str(tmp_path))

    result = db.merge_projects(conn, old["id"], new["id"])

    assert result["decisions"] == 1
    assert conn.execute("SELECT COUNT(*) FROM projects WHERE id = ?", (old["id"],)).fetchone()[0] == 0
    assert db.get_decision_request(conn, old_decision)["project_id"] == new["id"]
    # Same folder registered twice collapses to the surviving project's repository.
    assert db.get_decision_request(conn, old_decision)["repository_id"] == new_repo["id"]
    assert db.get_decision_request(conn, new_decision)["repository_id"] == new_repo["id"]
    assert conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 1
    assert db.list_sessions(conn)[0]["project_id"] == new["id"]
    assert db.get_or_create_project(conn, "Umbrella")["id"] == new["id"]
    files = {p.name for p in (tmp_path / ".claude" / "decisions").iterdir()}
    assert f"{old_decision:04d}-old-one.md" in files
    assert "# Decisions — Hooli" in (tmp_path / ".claude" / "decisions" / "INDEX.md").read_text()


def test_merge_projects_refuses_to_merge_a_project_into_itself(db_path):
    conn = db.get_connection()
    project = db.get_or_create_project(conn, "acme")
    try:
        db.merge_projects(conn, project["id"], project["id"])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_projects_sharing_a_folder_are_reported(db_path, tmp_path):
    conn = db.get_connection()
    a = db.get_or_create_project(conn, "Umbrella")
    b = db.get_or_create_project(conn, "Hooli")
    db.create_repository(conn, a["id"], "a", str(tmp_path) + "/")
    db.create_repository(conn, b["id"], "b", str(tmp_path))

    shared = db.projects_sharing_a_folder(conn)

    assert shared == [{"path": str(tmp_path), "projects": ["Hooli", "Umbrella"], "newest": "Hooli"}]
