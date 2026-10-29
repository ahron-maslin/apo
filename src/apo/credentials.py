"""Encrypted storage for integration credentials (API keys, tokens), replacing
hand-edited .env entries with a UI-manageable store. A DB-backed credential for a
provider takes precedence over the equivalent env-based config; the env fallback
stays supported for existing installs that never touch Settings.

Fernet key lives at config.get_secret_key_path(), next to the DB it protects —
generated on first use, written 0600. A corrupted/undecryptable row is treated as
"not configured" (returns None) rather than crashing the app, since that's exactly
how an unset credential already behaves everywhere this is read.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from apo import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_key() -> bytes:
    key_path = config.get_secret_key_path()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_EXCL makes "create" and "restrict to 0600" one atomic step — a
        # write-then-chmod sequence leaves the key briefly (or, if the process dies
        # in between, permanently) world/group-readable, and a concurrent first-time
        # caller (e.g. the poll loop's asyncio.to_thread alongside a Settings
        # request) would otherwise race to generate two different keys.
        fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return key_path.read_bytes()
    key = Fernet.generate_key()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def set_credentials(conn: sqlite3.Connection, provider: str, data: dict, display_name: str | None = None) -> None:
    encrypted = Fernet(_load_key()).encrypt(json.dumps(data).encode())
    now = _now()
    conn.execute(
        """
        INSERT INTO integration_credentials (provider, encrypted_secret, display_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(provider) DO UPDATE SET
            encrypted_secret = excluded.encrypted_secret,
            display_name = excluded.display_name,
            updated_at = excluded.updated_at
        """,
        (provider, encrypted, display_name, now, now),
    )
    conn.commit()


def get_credentials(conn: sqlite3.Connection, provider: str) -> dict | None:
    row = conn.execute(
        "SELECT encrypted_secret FROM integration_credentials WHERE provider = ?", (provider,)
    ).fetchone()
    if row is None:
        return None
    try:
        decrypted = Fernet(_load_key()).decrypt(bytes(row["encrypted_secret"]))
        return json.loads(decrypted)
    except Exception:  # noqa: BLE001 — any corrupt/unexpected row reads as "not configured"
        return None


def delete_credentials(conn: sqlite3.Connection, provider: str) -> None:
    conn.execute("DELETE FROM integration_credentials WHERE provider = ?", (provider,))
    conn.commit()


def get_display_name(conn: sqlite3.Connection, provider: str) -> str | None:
    row = conn.execute(
        "SELECT display_name FROM integration_credentials WHERE provider = ?", (provider,)
    ).fetchone()
    return row["display_name"] if row else None
