"""Tests for database schema and operations."""

import sqlite3

import pytest

from mediaman.db import init_db, get_db, DB_SCHEMA_VERSION


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
