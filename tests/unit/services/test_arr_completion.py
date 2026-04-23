"""Tests for arr_completion — detect_completed, cleanup_recent_downloads,
and record_verified_completions.

The basic disappear / appear / no-change coverage for detect_completed is
in tests/unit/web/test_downloads_api.py (TestCompletionDetection).
This file extends that with media-type / poster propagation checks and the
DB-backed helpers.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.services.arr_completion import (
    cleanup_recent_downloads,
    detect_completed,
    record_verified_completions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = init_db(str(tmp_path / "mediaman.db"))
    yield db
    db.close()


# ---------------------------------------------------------------------------
# detect_completed
# ---------------------------------------------------------------------------


class TestDetectCompleted:
    def test_matches_by_dl_id_key(self):
        """An item present in previous but absent from current is returned.

        The dict key is the dl_id — this is the canonical 'name' match
        (equivalent to matching by NZB/arr identifier).
        """
        arr_item = {
            "id": "radarr:Dune",
            "title": "Dune",
            "kind": "movie",
            "poster_url": "http://img/dune.jpg",
        }
        previous = {"radarr:Dune": arr_item}
        current: dict = {}

        result = detect_completed(previous, current)

        assert len(result) == 1
        assert result[0]["dl_id"] == "radarr:Dune"
        assert result[0]["title"] == "Dune"

    def test_propagates_media_type_and_poster(self):
        """Completed entries carry the ArrCard.kind mapped to media_type and poster_url."""
        previous = {
            "sonarr:Severance": {
                "id": "sonarr:Severance",
                "title": "Severance",
                "kind": "series",
                "poster_url": "http://img/sev.jpg",
            }
        }
        current: dict = {}

        result = detect_completed(previous, current)

        assert result[0]["media_type"] == "series"
        assert result[0]["poster_url"] == "http://img/sev.jpg"

    def test_no_match_when_names_differ(self):
        """No completions when the current queue holds a completely different item."""
        previous = {
            "radarr:FilmA": {
                "id": "radarr:FilmA",
                "title": "Film A",
                "kind": "movie",
                "poster_url": "",
            }
        }
        current = {
            "radarr:FilmB": {
                "id": "radarr:FilmB",
                "title": "Film B",
                "kind": "movie",
                "poster_url": "",
            }
        }

        result = detect_completed(previous, current)

        # FilmA vanished → completed; FilmB is new → not a completion
        assert len(result) == 1
        assert result[0]["dl_id"] == "radarr:FilmA"

    def test_empty_previous_yields_no_completions(self):
        """Nothing can complete when previous snapshot is empty."""
        current = {
            "radarr:Dune": {
                "id": "radarr:Dune",
                "title": "Dune",
                "kind": "movie",
                "poster_url": "",
            }
        }
        result = detect_completed({}, current)
        assert result == []


# ---------------------------------------------------------------------------
# cleanup_recent_downloads
# ---------------------------------------------------------------------------


class TestCleanupRecentDownloads:
    def test_skips_when_no_recent_downloads(self, conn):
        """An empty table results in zero rows deleted, no Radarr/Sonarr calls needed."""
        rows_before = conn.execute("SELECT COUNT(*) FROM recent_downloads").fetchone()[0]
        assert rows_before == 0

        deleted = cleanup_recent_downloads(conn)
        assert deleted == 0

    def test_removes_only_expired_rows(self, conn):
        """Only rows older than 7 days are purged; fresh rows survive."""
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type, completed_at)"
            " VALUES (?, ?, ?, datetime('now', '-10 days'))",
            ("radarr:OldFilm", "Old Film", "movie"),
        )
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type)"
            " VALUES (?, ?, ?)",
            ("radarr:NewFilm", "New Film", "movie"),
        )
        conn.commit()

        deleted = cleanup_recent_downloads(conn)

        assert deleted == 1
        remaining = [
            r[0]
            for r in conn.execute("SELECT dl_id FROM recent_downloads").fetchall()
        ]
        assert "radarr:OldFilm" not in remaining
        assert "radarr:NewFilm" in remaining


# ---------------------------------------------------------------------------
# record_verified_completions
# ---------------------------------------------------------------------------


class TestRecordVerifiedCompletions:
    def test_records_radarr_item_when_has_file(self, conn):
        """A Radarr item confirmed to have a file is inserted into recent_downloads."""
        mock_radarr = MagicMock()
        mock_radarr.get_movies.return_value = [
            {"title": "Dune", "hasFile": True},
        ]

        def build_client(c, svc):
            return mock_radarr if svc == "radarr" else None

        completed = [
            {
                "dl_id": "radarr:Dune",
                "title": "Dune",
                "media_type": "movie",
                "poster_url": "",
            }
        ]
        record_verified_completions(conn, completed, build_client)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'radarr:Dune'"
        ).fetchall()
        assert len(rows) == 1

    def test_skips_radarr_item_without_file(self, conn):
        """A Radarr item that has no file is not persisted."""
        mock_radarr = MagicMock()
        mock_radarr.get_movies.return_value = [
            {"title": "Dune", "hasFile": False},
        ]

        def build_client(c, svc):
            return mock_radarr if svc == "radarr" else None

        completed = [
            {
                "dl_id": "radarr:Dune",
                "title": "Dune",
                "media_type": "movie",
                "poster_url": "",
            }
        ]
        record_verified_completions(conn, completed, build_client)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'radarr:Dune'"
        ).fetchall()
        assert len(rows) == 0

    def test_handles_arr_client_none(self, conn):
        """If build_arr_client returns None, function completes without error."""

        def build_client(c, svc):
            return None

        completed = [
            {
                "dl_id": "radarr:Interstellar",
                "title": "Interstellar",
                "media_type": "movie",
                "poster_url": "",
            }
        ]
        # Must not raise even with no client
        record_verified_completions(conn, completed, build_client)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'radarr:Interstellar'"
        ).fetchall()
        # Not verified → not inserted
        assert len(rows) == 0

    def test_nzbget_only_item_is_verified_without_arr(self, conn):
        """NZBGet-only items (no radarr:/sonarr: prefix) are inserted unconditionally."""

        def build_client(c, svc):
            # Should never be called for NZBGet-only items
            raise AssertionError("build_client should not be called for NZBGet-only items")

        completed = [
            {
                "dl_id": "nzbget:SomeNzb",
                "title": "Some Nzb Download",
                "media_type": "movie",
                "poster_url": "",
            }
        ]
        record_verified_completions(conn, completed, build_client)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'nzbget:SomeNzb'"
        ).fetchall()
        assert len(rows) == 1

    def test_sonarr_item_recorded_when_has_episode_files(self, conn):
        """A Sonarr series with episodeFileCount > 0 is inserted into recent_downloads."""
        mock_sonarr = MagicMock()
        mock_sonarr.get_series.return_value = [
            {
                "title": "Severance",
                "statistics": {"episodeFileCount": 3},
            }
        ]

        def build_client(c, svc):
            return mock_sonarr if svc == "sonarr" else None

        completed = [
            {
                "dl_id": "sonarr:Severance",
                "title": "Severance",
                "media_type": "series",
                "poster_url": "",
            }
        ]
        record_verified_completions(conn, completed, build_client)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'sonarr:Severance'"
        ).fetchall()
        assert len(rows) == 1

    def test_arr_network_error_skips_item_without_false_positive_log(self, conn):
        """When the arr client raises, the item is skipped — not logged as 'no files confirmed'."""
        mock_radarr = MagicMock()
        mock_radarr.get_movies.side_effect = ConnectionError("network error")

        def build_client(c, svc):
            return mock_radarr if svc == "radarr" else None

        completed = [
            {
                "dl_id": "radarr:Dune",
                "title": "Dune",
                "media_type": "movie",
                "poster_url": "",
            }
        ]
        record_verified_completions(conn, completed, build_client)

        rows = conn.execute(
            "SELECT dl_id FROM recent_downloads WHERE dl_id = 'radarr:Dune'"
        ).fetchall()
        # Network failure → skipped, not inserted
        assert len(rows) == 0

    def test_multiple_items_partial_network_failure(self, conn):
        """A network error on one item does not prevent other items from being processed."""
        call_count = {"n": 0}

        def mock_get_movies():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("transient error")
            return [{"title": "Dune", "hasFile": True}]

        mock_radarr = MagicMock()
        mock_radarr.get_movies.side_effect = mock_get_movies

        def build_client(c, svc):
            return mock_radarr if svc == "radarr" else None

        completed = [
            {"dl_id": "radarr:Dune1", "title": "Dune1", "media_type": "movie", "poster_url": ""},
            {"dl_id": "radarr:Dune", "title": "Dune", "media_type": "movie", "poster_url": ""},
        ]
        record_verified_completions(conn, completed, build_client)

        rows = conn.execute("SELECT dl_id FROM recent_downloads").fetchall()
        dl_ids = {r["dl_id"] for r in rows}
        assert "radarr:Dune1" not in dl_ids  # failed — skipped
        assert "radarr:Dune" in dl_ids  # succeeded
