"""Tests for scan engine write-phase behaviour (dry_run suppression, added_at resolution)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

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


class TestRunScanDryRun:
    """D05 finding 1: dry_run must skip *every* mutating side-effect of a
    full scan, not just the on-disk rm.
    """

    def _stale_movie(self, key="100", days_old=60):
        now = datetime.now(UTC)
        return {
            "plex_rating_key": key,
            "title": f"Stale Movie {key}",
            "added_at": now - timedelta(days=days_old),
            "file_path": f"/media/movies/Stale {key}",
            "file_size_bytes": 1_000_000_000,
            "poster_path": None,
        }

    def test_dry_run_does_not_write_schedule_deletion(self, conn, mock_plex, freezer):
        """A stale movie that would normally schedule deletion must NOT
        write a ``scheduled_actions`` row when ``dry_run=True``.
        """
        mock_plex.get_movie_items.return_value = [self._stale_movie("100")]
        mock_plex.get_watch_history.return_value = []

        with (
            patch("mediaman.scanner.engine._send_newsletter") as mock_news,
            patch("mediaman.scanner.engine._refresh_recommendations") as mock_recs,
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["1"],
                library_types={"1": "movie"},
                secret_key="k",
                min_age_days=30,
                inactivity_days=30,
                dry_run=True,
            )
            result = engine.run_scan()

        assert result["scheduled"] == 1, "summary still reports the would-be schedule"
        # No scheduled_actions row written.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduled_actions WHERE action='scheduled_deletion'"
            ).fetchone()[0]
            == 0
        )
        # No audit_log entry from schedule_deletion either.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='scheduled_deletion'"
            ).fetchone()[0]
            == 0
        )
        # Newsletter and recommendations refresh must be skipped.
        mock_news.assert_not_called()
        mock_recs.assert_not_called()

    def test_dry_run_does_not_remove_orphans(self, conn, mock_plex, freezer):
        """Pre-existing media_items that are no longer in Plex must NOT
        be deleted in dry_run mode.
        """
        # Seed 50 prior items so the orphan-guard ratio check would
        # otherwise pass once Plex returns nothing.
        for i in range(50):
            insert_media_item(
                conn,
                id=f"orphan-{i}",
                title=f"Title {i}",
                plex_rating_key=f"orphan-{i}",
                added_at="2026-01-01",
                file_path=f"/media/{i}",
                file_size_bytes=1,
            )

        # Plex returns one current item — without the dry_run guard,
        # the other 49 would be eligible for orphan removal.
        mock_plex.get_movie_items.return_value = [self._stale_movie("orphan-0", days_old=5)]
        mock_plex.get_watch_history.return_value = []

        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["1"],
                library_types={"1": "movie"},
                secret_key="k",
                dry_run=True,
            )
            result = engine.run_scan()

        assert result["removed"] == 0
        # Original 50 items still present (the upsert may have updated
        # one row to match the Plex item, but no DELETE ran).
        assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 50

    def test_dry_run_does_not_send_newsletter(self, conn, mock_plex):
        """The mailer must not be invoked in dry_run mode (D05 finding 1)."""
        with (
            patch("mediaman.scanner.engine._send_newsletter") as mock_news,
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="k",
                dry_run=True,
            )
            engine.run_scan()

        mock_news.assert_not_called()

    def test_dry_run_does_not_clean_up_expired_snoozes(self, conn, mock_plex, freezer):
        """An expired snooze must not be deleted in dry_run mode —
        cleanup is a write that ``run_scan`` (via ``execute_deletions``)
        would normally perform but must be suppressed under a true
        preview (D05 finding 10).
        """
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()
        # Seed a media item + expired snooze.
        insert_media_item(
            conn,
            id="m1",
            title="Snoozed",
            plex_rating_key="m1",
            added_at=now,
            file_path="/m1",
            file_size_bytes=0,
        )
        insert_scheduled_action(
            conn, media_item_id="m1", action="snoozed", execute_at=past, token="tok-snz"
        )

        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="k",
                dry_run=True,
            )
            engine.run_scan()

        # The expired snooze row must still exist.
        row = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id='m1' AND action='snoozed'"
        ).fetchone()
        assert row is not None


class TestResolveAddedAt:
    """D05 findings 2 + 3: ``_resolve_added_at`` must prefer Arr date,
    then Plex ``added_at``, and only fall back to ``updated_at`` as a
    last resort. An unparseable Arr date must fall through, not be
    substituted with ``now()``.
    """

    def test_prefers_added_at_over_updated_at(self, conn, mock_plex, freezer):
        """When Plex ``updated_at`` was reset (e.g. subtitle download)
        but ``added_at`` is from years ago, eligibility must be measured
        against ``added_at``, not the recent metadata-refresh marker.
        """
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[],
            library_types={},
            secret_key="k",
        )
        old_added = datetime.now(UTC) - timedelta(days=400)
        recent_updated = datetime.now(UTC) - timedelta(hours=1)
        item = {
            "file_path": "/media/movies/Foo",
            "added_at": old_added,
            "updated_at": recent_updated,
        }
        resolved = engine._resolve_added_at(item)
        # Should be old_added (not recent_updated)
        assert abs((resolved - old_added).total_seconds()) < 2

    def test_unparseable_arr_date_falls_through_to_added_at(self, conn, mock_plex, freezer):
        """A bad Arr cache value used to be silently replaced by
        ``datetime.now(UTC)`` and gave the item permanent protection.
        It must now fall through to ``added_at`` so eligibility is
        evaluated normally (D05 finding 3).
        """
        from mediaman.scanner.arr_dates import normalise_path

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[],
            library_types={},
            secret_key="k",
        )
        # Pre-populate the cache so ensure_loaded() is a no-op and
        # never fires Radarr/Sonarr fetches.
        bad_path = "/media/movies/Bar"
        engine._arr_cache._dates = {normalise_path(bad_path): "not-a-date"}  # type: ignore[attr-defined]
        engine._arr_cache._loaded = True  # type: ignore[attr-defined]
        old_added = datetime.now(UTC) - timedelta(days=400)
        item = {"file_path": bad_path, "added_at": old_added}
        resolved = engine._resolve_added_at(item)
        assert abs((resolved - old_added).total_seconds()) < 2
