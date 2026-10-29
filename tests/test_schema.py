"""Connection setup and schema migration.

Every hook process (one per tool call, across every open session) opens a connection,
so what get_connection does on each open is on the hot path of every agent's tool loop.
"""
from datetime import datetime, timedelta, timezone

from apo import db


def test_connection_uses_wal_with_normal_sync(db_path):
    conn = db.get_connection()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # 1 == NORMAL: safe under WAL, and ~70x cheaper commits than FULL for hook writes.
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_connection_stamps_the_schema_version(db_path):
    conn = db.get_connection()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_schema_work_is_skipped_once_the_version_is_current(db_path, monkeypatch):
    db.get_connection()
    calls = []
    monkeypatch.setattr(db, "_apply_ddl", lambda conn: calls.append(1))
    db.get_connection()
    assert calls == []


def test_an_older_db_is_migrated_on_open(db_path):
    conn = db.get_connection()
    conn.execute("PRAGMA user_version = 0")
    conn.execute("DROP INDEX ix_sessions_status_seen")
    conn.commit()

    conn = db.get_connection()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "ix_sessions_status_seen" in names


def test_hot_path_indexes_exist(db_path):
    conn = db.get_connection()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {
        "ix_checklist_session", "ix_sessions_status_seen", "ix_sessions_project",
        "ix_dr_status_project", "ix_dr_origin_session", "ix_audit_dr", "ix_attach_dr",
        "ix_sync_errors_time",
    } <= names


def test_adding_a_column_that_another_process_just_added_is_not_an_error(db_path):
    """Two hook processes can both see a column missing and race to add it; the loser
    must not crash its hook (or, worse, the dashboard)."""
    conn = db.get_connection()
    conn.execute("ALTER TABLE projects ADD COLUMN race_test TEXT")
    db._add_column(conn, "projects", "race_test", "TEXT")


def test_list_sessions_ended_since_keeps_recent_ended_and_drops_old(db_path, tmp_path):
    conn = db.get_connection()
    db.register_session(conn, "live", str(tmp_path))
    db.register_session(conn, "recent", str(tmp_path))
    db.register_session(conn, "old", str(tmp_path))
    db.session_end(conn, "recent")
    db.session_end(conn, "old")
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn.execute("UPDATE sessions SET last_seen_at = ? WHERE claude_session_id = 'old'", (long_ago,))
    conn.commit()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    ids = {s["claude_session_id"] for s in db.list_sessions(conn, ended_since=cutoff)}
    assert ids == {"live", "recent"}


def test_checklists_for_many_sessions_come_back_in_one_call(db_path, tmp_path):
    conn = db.get_connection()
    a = db.register_session(conn, "a", str(tmp_path))
    b = db.register_session(conn, "b", str(tmp_path))
    db.add_checklist_item(conn, a, "first")
    db.add_checklist_item(conn, b, "second")
    db.add_checklist_item(conn, a, "third")

    by_session = db.list_checklist_items_for_sessions(conn, [a, b])
    assert [i["title"] for i in by_session[a]] == ["first", "third"]
    assert [i["title"] for i in by_session[b]] == ["second"]
    assert db.list_checklist_items_for_sessions(conn, []) == {}


def test_project_counts_counts_in_sql(db_path):
    conn = db.get_connection()
    acme = db.get_or_create_project(conn, "acme")
    db.create_decision_request(conn, project_id=acme["id"], title="t1", question="q")
    second = db.create_decision_request(conn, project_id=acme["id"], title="t2", question="q")
    db.answer_decision_request(conn, second, "yes")
    db.get_or_create_project(conn, "empty")

    counts = db.project_counts(conn)
    assert counts["acme"] == {"total": 2, "open": 1, "answered": 1, "archived": False}
    assert "empty" not in counts
