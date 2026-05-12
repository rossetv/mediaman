"""Tests for the Plex fetch phase of the scan engine (run_scan / sync_library)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.scanner.engine import ScanEngine
from tests.helpers.factories import insert_media_item, insert_scheduled_action


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


class TestScanEngine:
    def test_scan_schedules_stale_movie(self, conn, mock_plex, freezer):
        now = datetime.now(UTC)
        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "100",
                "title": "Old Movie",
                "added_at": now - timedelta(days=60),
                "file_path": "/media/movies/Old Movie (2020)",
                "file_size_bytes": 5_000_000_000,
                "poster_path": "/library/metadata/100/thumb/1",
            }
        ]
        mock_plex.get_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
            min_age_days=30,
            inactivity_days=30,
            grace_days=14,
        )
        result = engine.run_scan()

        assert result["scheduled"] == 1
        row = conn.execute("SELECT * FROM scheduled_actions").fetchone()
        assert row is not None
        assert row["action"] == "scheduled_deletion"

    def test_scan_skips_protected_items(self, conn, mock_plex, freezer):
        now = datetime.now(UTC)
        insert_media_item(
            conn,
            id="100",
            title="Protected Movie",
            plex_rating_key="100",
            added_at=now - timedelta(days=60),
            file_path="/media/movies/Protected",
            file_size_bytes=5_000_000_000,
        )
        insert_scheduled_action(
            conn, media_item_id="100", action="protected_forever", token="tok-123"
        )

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "100",
                "title": "Protected Movie",
                "added_at": now - timedelta(days=60),
                "file_path": "/media/movies/Protected",
                "file_size_bytes": 5_000_000_000,
                "poster_path": None,
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 0

    def test_scan_creates_media_item_record(self, conn, mock_plex, freezer):
        now = datetime.now(UTC)
        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "200",
                "title": "New Movie",
                "added_at": now - timedelta(days=5),
                "file_path": "/media/movies/New Movie",
                "file_size_bytes": 3_000_000_000,
                "poster_path": "/library/metadata/200/thumb/1",
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        engine.run_scan()

        row = conn.execute("SELECT * FROM media_items WHERE id='200'").fetchone()
        assert row is not None
        assert row["title"] == "New Movie"

    def test_scan_skips_already_scheduled(self, conn, mock_plex, freezer):
        """Items with an existing scheduled_deletion action are not re-scheduled."""
        now = datetime.now(UTC)
        insert_media_item(
            conn,
            id="300",
            title="Already Scheduled",
            plex_rating_key="300",
            added_at=now - timedelta(days=90),
            file_path="/media/movies/Scheduled",
            file_size_bytes=2_000_000_000,
        )
        insert_scheduled_action(conn, media_item_id="300", token="tok-already")

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "300",
                "title": "Already Scheduled",
                "added_at": now - timedelta(days=90),
                "file_path": "/media/movies/Scheduled",
                "file_size_bytes": 2_000_000_000,
                "poster_path": None,
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 0
        count = conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE media_item_id='300'"
        ).fetchone()[0]
        assert count == 1  # no duplicate inserted

    def test_scan_flags_reentry(self, conn, mock_plex, freezer):
        """An item whose snooze has expired is scheduled and marked as re-entry."""
        now = datetime.now(UTC)
        insert_media_item(
            conn,
            id="400",
            title="Snoozed Movie",
            plex_rating_key="400",
            added_at=now - timedelta(days=120),
            file_path="/media/movies/Snoozed",
            file_size_bytes=4_000_000_000,
        )
        # A snooze that has already expired (token_used=1, execute_at in the past)
        insert_scheduled_action(
            conn,
            media_item_id="400",
            action="snoozed",
            scheduled_at=(now - timedelta(days=60)).isoformat(),
            execute_at=(now - timedelta(days=30)).isoformat(),
            token="tok-old",
            token_used=True,
        )

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "400",
                "title": "Snoozed Movie",
                "added_at": now - timedelta(days=120),
                "file_path": "/media/movies/Snoozed",
                "file_size_bytes": 4_000_000_000,
                "poster_path": None,
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 1
        row = conn.execute(
            "SELECT * FROM scheduled_actions WHERE action='scheduled_deletion'"
        ).fetchone()
        assert row is not None
        assert row["is_reentry"] == 1

    def test_scan_returns_summary_counts(self, conn, mock_plex):
        """run_scan returns a dict with scanned, scheduled, skipped, errors keys."""
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert "scanned" in result
        assert "scheduled" in result
        assert "skipped" in result
        assert result["scanned"] == 0
        assert result["scheduled"] == 0

    def test_scan_tv_season(self, conn, mock_plex, freezer):
        """A stale TV season is scheduled for deletion."""
        now = datetime.now(UTC)
        mock_plex.get_show_seasons.return_value = [
            {
                "plex_rating_key": "500",
                "title": "Breaking Bad",
                "show_title": "Breaking Bad",
                "season_number": 1,
                "added_at": now - timedelta(days=90),
                "file_path": "/media/tv/Breaking Bad/Season 01",
                "file_size_bytes": 10_000_000_000,
                "poster_path": "/library/metadata/500/thumb/1",
                "episode_count": 7,
                "show_rating_key": "501",
            }
        ]
        mock_plex.get_season_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
            min_age_days=30,
            inactivity_days=30,
        )
        result = engine.run_scan()
        assert result["scheduled"] == 1
        row = conn.execute("SELECT * FROM scheduled_actions").fetchone()
        assert row is not None
        assert row["action"] == "scheduled_deletion"

    def test_scan_audit_log_entry(self, conn, mock_plex, freezer):
        """Each scheduled item produces an audit_log entry."""
        now = datetime.now(UTC)
        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "600",
                "title": "Audit Movie",
                "added_at": now - timedelta(days=60),
                "file_path": "/media/movies/Audit",
                "file_size_bytes": 1_000_000_000,
                "poster_path": None,
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        engine.run_scan()

        row = conn.execute("SELECT * FROM audit_log WHERE media_item_id='600'").fetchone()
        assert row is not None
        assert row["action"] == "scheduled_deletion"

    def test_scan_returns_deleted_and_reclaimed(self, conn, mock_plex):
        """run_scan summary includes deleted and reclaimed_bytes keys."""
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert "deleted" in result
        assert "reclaimed_bytes" in result
        assert result["deleted"] == 0
        assert result["reclaimed_bytes"] == 0

    def test_scan_respects_newsletter_snooze(self, conn, mock_plex, freezer):
        """Items snoozed via the newsletter keep flow (token_used=1, future execute_at) are not re-scheduled."""
        now = datetime.now(UTC)
        future = (now + timedelta(days=25)).isoformat()
        insert_media_item(
            conn,
            id="1001",
            title="Kept Movie",
            plex_rating_key="1001",
            added_at=now - timedelta(days=90),
            file_path="/media/movies/Kept",
            file_size_bytes=3_000_000_000,
        )
        insert_scheduled_action(
            conn,
            media_item_id="1001",
            action="snoozed",
            execute_at=future,
            token="tok-newsletter",
            token_used=True,
        )

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "1001",
                "title": "Kept Movie",
                "added_at": now - timedelta(days=90),
                "file_path": "/media/movies/Kept",
                "file_size_bytes": 3_000_000_000,
                "poster_path": None,
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 0

    def test_scan_reschedules_expired_snooze(self, conn, mock_plex, freezer):
        """Items with an expired snooze (token_used=1, past execute_at) are scheduled for deletion."""
        now = datetime.now(UTC)
        past = (now - timedelta(days=5)).isoformat()
        insert_media_item(
            conn,
            id="1002",
            title="Expired Snooze Movie",
            plex_rating_key="1002",
            added_at=now - timedelta(days=120),
            file_path="/media/movies/ExpiredSnooze",
            file_size_bytes=5_000_000_000,
        )
        insert_scheduled_action(
            conn,
            media_item_id="1002",
            action="snoozed",
            scheduled_at=(now - timedelta(days=35)).isoformat(),
            execute_at=past,
            token="tok-expired",
            token_used=True,
        )

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "1002",
                "title": "Expired Snooze Movie",
                "added_at": now - timedelta(days=120),
                "file_path": "/media/movies/ExpiredSnooze",
                "file_size_bytes": 5_000_000_000,
                "poster_path": None,
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 1

    def test_plex_datetime_stored_as_correct_utc(self, conn, mock_plex):
        """Plex datetimes must round-trip to UTC without offset drift.

        The Plex client (`media_meta/plex.py:_to_utc`) now promotes
        plexapi's naive local-time datetimes to tz-aware UTC at the
        boundary, so by the time `engine._resolve_added_at` sees them
        they are already aware. This test verifies the engine stores
        the aware UTC value unchanged (no `.replace(tzinfo=UTC)`
        mislabelling, no double-conversion).
        """
        posix_ts = 1744400000  # 2025-04-11 ~19:33 UTC
        aware_utc = datetime.fromtimestamp(posix_ts, tz=UTC)

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "9999",
                "title": "Timezone Test",
                "added_at": aware_utc,
                "file_path": "/media/movies/Timezone Test (2025)",
                "file_size_bytes": 1_500_000_000,
                "poster_path": None,
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        engine.sync_library()

        row = conn.execute("SELECT added_at FROM media_items WHERE id='9999'").fetchone()
        stored = datetime.fromisoformat(row["added_at"])
        expected_utc = datetime.fromtimestamp(posix_ts, tz=UTC)

        assert abs((stored - expected_utc).total_seconds()) < 2
