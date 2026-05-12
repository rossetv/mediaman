"""Tests for show-level keep rules, per-library orphan guard, and unknown library types."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.scanner.engine import ScanEngine
from tests.helpers.factories import insert_kept_show, insert_media_item


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


@pytest.fixture
def mock_plex():
    client = MagicMock()
    client.get_movie_items.return_value = []
    client.get_show_seasons.return_value = []
    client.get_watch_history.return_value = []
    client.get_season_watch_history.return_value = []
    return client


class TestShowLevelKeep:
    """Tests for show-level keep rules via kept_shows table."""

    def test_show_rating_key_stored_during_tv_scan(self, conn, mock_plex, freezer):
        now = datetime.now(UTC)
        mock_plex.get_show_seasons.return_value = [
            {
                "plex_rating_key": "600",
                "title": "Test Show",
                "show_title": "Test Show",
                "season_number": 1,
                "added_at": now - timedelta(days=5),
                "file_path": "/media/tv/Test Show/Season 01",
                "file_size_bytes": 5_000_000_000,
                "poster_path": "/library/metadata/600/thumb/1",
                "episode_count": 10,
                "show_rating_key": "599",
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
        )
        engine.run_scan()

        row = conn.execute("SELECT show_rating_key FROM media_items WHERE id='600'").fetchone()
        assert row is not None
        assert row["show_rating_key"] == "599"

    def test_kept_show_skips_all_seasons(self, conn, mock_plex, freezer):
        now = datetime.now(UTC)
        insert_kept_show(
            conn, show_rating_key="599", show_title="Test Show", action="protected_forever"
        )

        mock_plex.get_show_seasons.return_value = [
            {
                "plex_rating_key": "600",
                "title": "Test Show",
                "show_title": "Test Show",
                "season_number": 1,
                "added_at": now - timedelta(days=90),
                "file_path": "/media/tv/Test Show/Season 01",
                "file_size_bytes": 5_000_000_000,
                "poster_path": None,
                "episode_count": 10,
                "show_rating_key": "599",
            }
        ]
        mock_plex.get_season_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 0
        assert result["skipped"] == 1

    def test_expired_show_snooze_allows_scan(self, conn, mock_plex, freezer):
        now = datetime.now(UTC)
        past = (now - timedelta(days=1)).isoformat()
        insert_kept_show(
            conn, show_rating_key="599", show_title="Test Show", action="snoozed", execute_at=past
        )

        mock_plex.get_show_seasons.return_value = [
            {
                "plex_rating_key": "600",
                "title": "Test Show",
                "show_title": "Test Show",
                "season_number": 1,
                "added_at": now - timedelta(days=90),
                "file_path": "/media/tv/Test Show/Season 01",
                "file_size_bytes": 5_000_000_000,
                "poster_path": None,
                "episode_count": 10,
                "show_rating_key": "599",
            }
        ]
        mock_plex.get_season_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 1

        # Expired row should be cleaned up
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='599'").fetchone()
        assert row is None


class TestPerLibraryOrphanGuard:
    """D05 finding 7: orphan-removal numerical safeguards must evaluate
    each library independently. A library that returns 0 items must not
    be wiped just because a sibling library returned 1000.
    """

    def _populate_items(self, conn, lib_id, n):
        for i in range(n):
            insert_media_item(
                conn,
                id=f"item-{lib_id}-{i}",
                title=f"t-{i}",
                plex_library_id=lib_id,
                plex_rating_key=f"item-{lib_id}-{i}",
                added_at="2026-01-01",
                file_path=f"/media/{i}",
                file_size_bytes=1,
            )

    def test_empty_library_does_not_wipe_when_sibling_full(
        self,
        conn,
        mock_plex,
        caplog,
        freezer,
    ):
        """Library 7 had 50 items, library 8 had 0 returned by Plex,
        library 9 returned 1000. Pre-fix the union (1000+0=1000 > 0.10
        of 50+1000=1050 = 105) passed, so library 8's 50 items were
        wiped. Per-library evaluation must protect them.
        """
        self._populate_items(conn, 7, 50)
        self._populate_items(conn, 8, 50)

        def get_movies(lib_id):
            if lib_id == "7":
                return [
                    {
                        "plex_rating_key": f"item-7-{i}",
                        "title": f"t-{i}",
                        "added_at": datetime.now(UTC) - timedelta(days=5),
                        "file_path": f"/media/{i}",
                        "file_size_bytes": 1,
                        "poster_path": None,
                    }
                    for i in range(50)
                ]
            if lib_id == "8":
                return []  # auth hiccup — should be skipped/preserved
            if lib_id == "9":
                return [
                    {
                        "plex_rating_key": f"new-9-{i}",
                        "title": f"new-{i}",
                        "added_at": datetime.now(UTC) - timedelta(days=5),
                        "file_path": f"/media/new-{i}",
                        "file_size_bytes": 1,
                        "poster_path": None,
                    }
                    for i in range(1000)
                ]
            return []

        mock_plex.get_movie_items.side_effect = get_movies

        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["7", "8", "9"],
                library_types={"7": "movie", "8": "movie", "9": "movie"},
                secret_key="k",
                min_age_days=999_999,
            )
            with caplog.at_level("WARNING", logger="mediaman"):
                engine.run_scan()

        assert (
            conn.execute("SELECT COUNT(*) FROM media_items WHERE plex_library_id=8").fetchone()[0]
            == 50
        )
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "below_min_items" in msgs


class TestUnknownLibraryType:
    """D05 finding 6: a library with no type mapping must be skipped
    loudly, not scanned as 'movie' by default.
    """

    def test_unknown_library_type_is_skipped_with_warning(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["1"],
                library_types={},  # no mapping for "1"
                secret_key="k",
            )
            with caplog.at_level("WARNING", logger="mediaman"):
                result = engine.run_scan()

        assert result["scanned"] == 0
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "unknown_library_type" in msgs

    def test_music_library_type_is_skipped_with_warning(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["3"],
                library_types={"3": "music"},
                secret_key="k",
            )
            with caplog.at_level("WARNING", logger="mediaman"):
                result = engine.run_scan()

        assert result["scanned"] == 0
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "unsupported_library_type" in msgs
