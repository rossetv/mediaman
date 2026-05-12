"""Tests for the recent_downloads database table and cleanup logic.

Covers:
- Migration v9: table exists with expected columns and UNIQUE constraint
- cleanup_recent_downloads: purges rows older than 7 days
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.db import init_db
from tests.helpers.factories import insert_recent_download


class TestRecentDownloadsTable:
    def test_recent_downloads_table_exists(self, db_path):
        """Migration v9 creates the recent_downloads table."""
        conn = init_db(str(db_path))
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        assert "recent_downloads" in tables

    def test_recent_downloads_columns(self, db_path):
        """recent_downloads has the expected columns."""
        conn = init_db(str(db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(recent_downloads)").fetchall()]
        assert "dl_id" in cols
        assert "title" in cols
        assert "media_type" in cols
        assert "poster_url" in cols
        assert "completed_at" in cols

    def test_recent_downloads_unique_dl_id(self, db_path):
        """dl_id has a UNIQUE constraint — duplicate inserts fail."""
        conn = init_db(str(db_path))
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES (?, ?, ?)",
            ("radarr:Dune", "Dune", "movie"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES (?, ?, ?)",
                ("radarr:Dune", "Dune", "movie"),
            )


class TestRecentDownloadsCleanup:
    def test_cleanup_removes_old_rows(self, db_path):
        """Rows older than 7 days are purged."""
        conn = init_db(str(db_path))
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        # Insert a row dated 10 days ago
        insert_recent_download(
            conn,
            dl_id="radarr:Old",
            title="Old Movie",
            media_type="movie",
            completed_at=ten_days_ago,
        )
        # Insert a row from today
        insert_recent_download(conn, dl_id="radarr:New", title="New Movie", media_type="movie")

        from mediaman.services.arr.completion import cleanup_recent_downloads

        cleanup_recent_downloads(conn)

        rows = conn.execute("SELECT dl_id FROM recent_downloads").fetchall()
        dl_ids = [r["dl_id"] for r in rows]
        assert "radarr:Old" not in dl_ids
        assert "radarr:New" in dl_ids
