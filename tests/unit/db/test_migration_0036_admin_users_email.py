"""0036 migration: nullable email column on admin_users."""

from __future__ import annotations

import importlib
import sqlite3

import pytest

from mediaman.db.schema_definition import CUTOVER_VERSION


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


@pytest.fixture
def conn_at_v35() -> sqlite3.Connection:
    """A connection holding the pre-0036 schema (v35).

    We start at the cutover baseline (v34) and apply the registered 0035
    migration so the test fixture matches what a freshly-upgraded 1.8.x
    database looks like just before 0036 runs.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE admin_users ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  username TEXT UNIQUE NOT NULL,"
        "  password_hash TEXT NOT NULL,"
        "  created_at TEXT NOT NULL,"
        "  must_change_password INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute("PRAGMA user_version=35")
    conn.commit()
    return conn


def test_0036_adds_nullable_email_column(conn_at_v35: sqlite3.Connection) -> None:
    mod = importlib.import_module("mediaman.db.migrations.0036_admin_users_email")
    mod.apply(conn_at_v35)
    cols = _column_names(conn_at_v35, "admin_users")
    assert "email" in cols
    info = conn_at_v35.execute(
        "SELECT \"notnull\", dflt_value FROM pragma_table_info('admin_users') WHERE name='email'"
    ).fetchone()
    assert info[0] == 0, "email column must be nullable"
    assert info[1] is None, "email column must have no default"


def test_0036_preserves_existing_rows(conn_at_v35: sqlite3.Connection) -> None:
    conn_at_v35.execute(
        "INSERT INTO admin_users (username, password_hash, created_at) "
        "VALUES ('rossetv', 'fakehash', '2024-12-01T00:00:00Z')"
    )
    conn_at_v35.commit()
    mod = importlib.import_module("mediaman.db.migrations.0036_admin_users_email")
    mod.apply(conn_at_v35)
    row = conn_at_v35.execute(
        "SELECT username, email FROM admin_users WHERE username='rossetv'"
    ).fetchone()
    assert row["username"] == "rossetv"
    assert row["email"] is None


def test_full_apply_migrations_to_36() -> None:
    """Fresh DB applied through the registry lands at v36 with the column."""
    from mediaman.db.migrations import apply_migrations
    from mediaman.db.schema_definition import DB_SCHEMA_VERSION

    assert DB_SCHEMA_VERSION >= 36
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == DB_SCHEMA_VERSION
    cols = _column_names(conn, "admin_users")
    assert "email" in cols


def test_cutover_version_unchanged() -> None:
    """The cutover floor never moves; only post-cutover migrations are added."""
    assert CUTOVER_VERSION == 34
