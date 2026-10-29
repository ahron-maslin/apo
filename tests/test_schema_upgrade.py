from apo import db

NEW_COLUMNS = [("sessions", "verdict"), ("sessions", "parent_session_id"), ("decision_requests", "completion"),
               ("decision_requests", "work_rank"), ("decision_requests", "undo_snapshot")]
NEW_TABLES = ["task_attempts", "saved_views", "item_notes"]


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_a_database_at_the_previous_schema_version_gets_the_new_columns_and_tables(db_path):
    conn = db.get_connection()
    for table, column in NEW_COLUMNS:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    for table in NEW_TABLES:
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version = 23")
    conn.commit()
    conn.close()

    conn = db.get_connection()

    for table, column in NEW_COLUMNS:
        assert column in _columns(conn, table)
    for table in NEW_TABLES:
        assert _columns(conn, table)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
