"""Tests for database schema and operations."""

import sqlite3
import threading

import pytest

from mediaman.db import (
    DB_SCHEMA_VERSION,
    close_db,
    get_db,
    init_db,
    set_connection,
)


class TestInitDb:
    def test_creates_tables(self, db_path):
        conn = init_db(str(db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        assert "admin_sessions" in tables
        assert "admin_users" in tables
        assert "audit_log" in tables
        assert "media_items" in tables
        assert "scheduled_actions" in tables
        assert "settings" in tables
        assert "subscribers" in tables

    def test_creates_schema_version(self, db_path):
        conn = init_db(str(db_path))
        cursor = conn.execute("PRAGMA user_version")
        version = cursor.fetchone()[0]
        assert version == DB_SCHEMA_VERSION

    def test_idempotent_init(self, db_path):
        conn1 = init_db(str(db_path))
        conn1.close()
        conn2 = init_db(str(db_path))
        cursor = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] for row in cursor.fetchall()]
        assert "media_items" in tables

    def test_wal_mode_enabled(self, db_path):
        conn = init_db(str(db_path))
        cursor = conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_enabled(self, db_path):
        conn = init_db(str(db_path))
        cursor = conn.execute("PRAGMA foreign_keys")
        enabled = cursor.fetchone()[0]
        assert enabled == 1


class TestSchemaV3:
    def test_media_items_has_show_rating_key(self, db_path):
        conn = init_db(str(db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(media_items)").fetchall()]
        assert "show_rating_key" in cols
        conn.close()

    def test_kept_shows_table_exists(self, db_path):
        conn = init_db(str(db_path))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "kept_shows" in tables
        conn.close()

    def test_kept_shows_columns(self, db_path):
        conn = init_db(str(db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(kept_shows)").fetchall()]
        assert "show_rating_key" in cols
        assert "show_title" in cols
        assert "action" in cols
        assert "execute_at" in cols
        conn.close()


class TestThreadLocalConnection:
    """Regression: ``get_db()`` must return a per-thread connection.

    Previously a single module-global connection was shared across the
    scanner, scheduler, and web threads. ``conn.commit()`` on one
    thread could then commit another thread's pending writes, and
    long transactions could block unrelated work. These tests confirm
    each thread now gets an isolated connection pointed at the same
    DB file.
    """

    def test_separate_connections_per_thread(self, db_path):
        conn_main = init_db(str(db_path))
        set_connection(conn_main)

        observed: dict[str, sqlite3.Connection | None] = {"worker": None}

        def worker() -> None:
            observed["worker"] = get_db()
            close_db()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert observed["worker"] is not None
        # The worker thread must not receive the main-thread connection.
        assert observed["worker"] is not conn_main

    def test_each_thread_sees_same_schema(self, db_path):
        """Both threads point at the same DB file — schema is shared."""
        conn_main = init_db(str(db_path))
        set_connection(conn_main)

        # Write something in the main thread.
        conn_main.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('thread_test', 'ok', 0, '2026-01-01')"
        )
        conn_main.commit()

        found: dict[str, str | None] = {"value": None}

        def worker() -> None:
            conn = get_db()
            row = conn.execute(
                "SELECT value FROM settings WHERE key='thread_test'"
            ).fetchone()
            found["value"] = row["value"] if row else None
            close_db()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert found["value"] == "ok"

    def test_close_db_only_touches_thread_local(self, db_path):
        """close_db() must not close the bootstrap connection."""
        conn_main = init_db(str(db_path))
        set_connection(conn_main)

        def worker() -> None:
            get_db()  # lazily open this thread's conn
            close_db()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # The main-thread connection must still be usable.
        row = conn_main.execute("SELECT 1").fetchone()
        assert row[0] == 1


class TestSchemaV13LegacySessionPurge:
    """C9 / C10: migration v13 hoists the session columns and purges
    legacy rows that pre-date the token-hashing hardening."""

    def test_purges_rows_with_null_token_hash(self, tmp_path):
        """A session row with ``token_hash IS NULL`` is unreachable under
        the new scheme and was also issued under the 7-day hard expiry.
        It must be deleted by the migration — forcing the user to log
        in again so they end up on the hardened path."""
        db_path = tmp_path / "legacy.db"
        # Hand-craft a v12 DB with a legacy session row.
        conn_old = sqlite3.connect(str(db_path))
        conn_old.row_factory = sqlite3.Row
        conn_old.executescript("""
            CREATE TABLE admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE admin_sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                encrypted INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            INSERT INTO admin_users (username, password_hash, created_at)
                VALUES ('legacy', 'x', '2026-01-01');
            INSERT INTO admin_sessions (token, username, created_at, expires_at)
                VALUES ('legacy-raw-token', 'legacy', '2026-01-01', '2099-12-31');
        """)
        conn_old.execute("PRAGMA user_version=12")
        conn_old.commit()
        conn_old.close()

        conn = init_db(str(db_path))
        # Legacy row must have been purged.
        rows = conn.execute("SELECT * FROM admin_sessions").fetchall()
        assert rows == []
        # New columns exist on the table.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(admin_sessions)").fetchall()}
        assert {"token_hash", "last_used_at", "fingerprint", "issued_ip"} <= cols
        # Version bumped.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION

    def test_purges_rows_with_expiry_over_one_day_cap(self, tmp_path):
        """Any session whose stored expiry is more than the new 1-day
        cap past its created_at gets purged — defensive against
        long-lived rows crafted directly in the DB or left from the old
        7-day hard expiry."""
        db_path = tmp_path / "legacy.db"
        # Start from a fresh v12 DB so we control the state precisely.
        conn = init_db(str(db_path))
        # Drop back to v12 and write a row with a 7-day TTL + populated
        # token_hash (so the null-hash branch doesn't catch it).
        conn.execute("PRAGMA user_version=12")
        conn.execute("DELETE FROM admin_sessions")
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) "
            "VALUES ('legacy', 'x', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO admin_sessions "
            "(token, token_hash, username, created_at, expires_at) "
            "VALUES ('tok', 'hash', 'legacy', '2026-01-01T00:00:00+00:00', "
            "'2026-01-08T00:00:00+00:00')"  # 7 days > 1 day cap
        )
        conn.commit()
        conn.close()

        # Re-open → migration v13 runs.
        conn = init_db(str(db_path))
        rows = conn.execute("SELECT * FROM admin_sessions").fetchall()
        assert rows == []

    def test_keeps_rows_within_cap(self, tmp_path):
        """A well-formed row inside the 1-day cap must be left alone."""
        db_path = tmp_path / "legacy.db"
        conn = init_db(str(db_path))
        conn.execute("PRAGMA user_version=12")
        conn.execute("DELETE FROM admin_sessions")
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) "
            "VALUES ('keep', 'x', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO admin_sessions "
            "(token, token_hash, username, created_at, expires_at) "
            "VALUES ('tok', 'h', 'keep', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T12:00:00+00:00')"  # 12 h < 1 day cap
        )
        conn.commit()
        conn.close()

        conn = init_db(str(db_path))
        rows = conn.execute(
            "SELECT username FROM admin_sessions"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["username"] == "keep"

    def test_migration_idempotent(self, db_path):
        """Running init_db twice must not error (migration re-runs are
        guarded)."""
        init_db(str(db_path)).close()
        init_db(str(db_path)).close()  # must not raise

    def test_schema_version_is_13(self, db_path):
        assert DB_SCHEMA_VERSION == 13
        conn = init_db(str(db_path))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
