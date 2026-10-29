import stat

from apo import db


def test_round_trip_encrypts_and_decrypts(db_path, credentials_module):
    conn = db.get_connection()
    credentials_module.set_credentials(
        conn, "linear", {"api_key": "key-123", "team": "ENG"}, display_name="Test User → Engineering"
    )

    assert credentials_module.get_credentials(conn, "linear") == {"api_key": "key-123", "team": "ENG"}
    assert credentials_module.get_display_name(conn, "linear") == "Test User → Engineering"


def test_get_credentials_returns_none_when_not_configured(db_path, credentials_module):
    conn = db.get_connection()
    assert credentials_module.get_credentials(conn, "linear") is None
    assert credentials_module.get_display_name(conn, "linear") is None


def test_set_credentials_upserts_by_provider(db_path, credentials_module):
    conn = db.get_connection()
    credentials_module.set_credentials(conn, "linear", {"api_key": "first", "team": "ENG"}, display_name="First")
    credentials_module.set_credentials(conn, "linear", {"api_key": "second", "team": "ENG2"}, display_name="Second")

    assert credentials_module.get_credentials(conn, "linear") == {"api_key": "second", "team": "ENG2"}
    assert credentials_module.get_display_name(conn, "linear") == "Second"
    count = conn.execute(
        "SELECT COUNT(*) FROM integration_credentials WHERE provider = ?", ("linear",)
    ).fetchone()[0]
    assert count == 1


def test_delete_credentials_removes_the_row(db_path, credentials_module):
    conn = db.get_connection()
    credentials_module.set_credentials(conn, "linear", {"api_key": "key-123"}, display_name="x")
    credentials_module.delete_credentials(conn, "linear")

    assert credentials_module.get_credentials(conn, "linear") is None
    assert credentials_module.get_display_name(conn, "linear") is None


def test_get_credentials_returns_none_for_corrupted_row_instead_of_raising(db_path, credentials_module):
    conn = db.get_connection()
    credentials_module.set_credentials(conn, "linear", {"api_key": "key-123", "team": "ENG"}, display_name="x")
    conn.execute(
        "UPDATE integration_credentials SET encrypted_secret = ? WHERE provider = ?",
        (b"not-a-valid-fernet-token", "linear"),
    )
    conn.commit()

    assert credentials_module.get_credentials(conn, "linear") is None


def test_get_credentials_returns_none_for_wrong_type_in_column_instead_of_raising(db_path, credentials_module):
    """A str landing in a BLOB column (e.g. a manual edit or a future migration
    bug) makes bytes(row["encrypted_secret"]) raise TypeError, not
    InvalidToken/ValueError — the module's documented "never crashes on a
    corrupted row" guarantee must cover this too."""
    conn = db.get_connection()
    credentials_module.set_credentials(conn, "linear", {"api_key": "key-123", "team": "ENG"}, display_name="x")
    conn.execute(
        "UPDATE integration_credentials SET encrypted_secret = ? WHERE provider = ?",
        ("not-bytes-at-all", "linear"),
    )
    conn.commit()

    assert credentials_module.get_credentials(conn, "linear") is None


def test_secret_key_file_created_with_owner_only_permissions(db_path, credentials_module):
    from apo import config

    conn = db.get_connection()
    credentials_module.set_credentials(conn, "linear", {"api_key": "key-123"}, display_name=None)

    key_path = config.get_secret_key_path()
    assert key_path.exists()
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600


def test_secret_key_reused_not_regenerated_across_calls(db_path, credentials_module):
    conn = db.get_connection()
    credentials_module.set_credentials(conn, "linear", {"api_key": "a"}, display_name=None)
    first = credentials_module.get_credentials(conn, "linear")

    credentials_module.set_credentials(conn, "jira", {"api_key": "b"}, display_name=None)
    second_linear = credentials_module.get_credentials(conn, "linear")

    assert first == second_linear == {"api_key": "a"}
