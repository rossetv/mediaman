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
