"""sync_errors is a failure log, not telemetry: the live DB had 1,701 rows, 1,575 of
them one proxy outage repeated every poll. A repeat of the same unresolved failure
is one row with a count."""
from apo import db


def test_a_repeated_failure_is_one_row_with_a_count(db_path):
    conn = db.get_connection()
    for _ in range(3):
        db.record_sync_error(conn, "linear_poll", None, "407 Proxy Authentication Required")

    rows = db.recent_sync_errors(conn, limit=10)
    assert len(rows) == 1
    assert rows[0]["count"] == 3
    assert rows[0]["first_occurred_at"] <= rows[0]["occurred_at"]


def test_different_failures_stay_separate(db_path):
    conn = db.get_connection()
    db.record_sync_error(conn, "linear_poll", None, "timeout")
    db.record_sync_error(conn, "linear_poll", None, "407")
    db.record_sync_error(conn, "linear_push", None, "timeout")
    db.record_sync_error(conn, "linear_poll", 4, "timeout")

    assert len(db.recent_sync_errors(conn, limit=10)) == 4


def test_a_failure_after_its_resolution_is_a_new_row(db_path):
    conn = db.get_connection()
    db.record_sync_error(conn, "linear_poll", None, "timeout")
    conn.execute("UPDATE sync_errors SET occurred_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    db.resolve_sync_errors(conn, ["linear_poll"], before=db._now())

    db.record_sync_error(conn, "linear_poll", None, "timeout")

    rows = db.recent_sync_errors(conn, limit=10)
    assert len(rows) == 2
    assert [r["count"] for r in rows] == [1, 1]


def test_upgrading_collapses_existing_duplicates(db_path):
    conn = db.get_connection()
    conn.execute("PRAGMA user_version = 3")
    for i in range(5):
        conn.execute(
            "INSERT INTO sync_errors (occurred_at, source, decision_request_id, message) VALUES (?, ?, ?, ?)",
            (f"2026-07-23T00:0{i}:00+00:00", "linear_poll", None, "407"),
        )
    conn.execute(
        "INSERT INTO sync_errors (occurred_at, source, decision_request_id, message) VALUES (?, ?, ?, ?)",
        ("2026-07-24T00:00:00+00:00", "linear_push", 3, "boom"),
    )
    conn.commit()

    conn = db.get_connection()

    rows = {r["source"]: r for r in db.recent_sync_errors(conn, limit=10)}
    assert len(rows) == 2
    assert rows["linear_poll"]["count"] == 5
    assert rows["linear_poll"]["first_occurred_at"] == "2026-07-23T00:00:00+00:00"
    assert rows["linear_poll"]["occurred_at"] == "2026-07-23T00:04:00+00:00"
    assert rows["linear_push"]["count"] == 1
