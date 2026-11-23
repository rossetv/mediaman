"""Tests for scan engine write-phase behaviour (dry_run suppression, added_at resolution)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.db import init_db
from mediaman.scanner.engine import ScanEngine
from mediaman.services.infra import SafeHTTPError
from mediaman.services.mail.newsletter import NewsletterConfigError
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
    """``dry_run`` must skip *every* mutating side-effect of a
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
        """The mailer must not be invoked in dry_run mode."""
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
        preview.
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
    """``_resolve_added_at`` must prefer Arr date,
    then Plex ``added_at``, and otherwise fall straight to ``now`` — it must
    NOT consult ``updated_at`` (a metadata-refresh marker), so the eligibility
    clock agrees with the stored ``media_items.added_at``. An unparseable Arr
    date must fall through, not be substituted with ``now()``.
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
        evaluated normally.
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

    def test_missing_added_at_does_not_fall_back_to_updated_at(self, conn, mock_plex, freezer):
        """With no Arr date and no ``added_at``, ``updated_at`` must be
        ignored entirely and the clock falls straight to ``now`` — so a
        subtitle/poster refresh can never keep an item permanently young, and
        the eligibility clock matches the ``now``-stored ``media_items.added_at``.
        """
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[],
            library_types={},
            secret_key="k",
        )
        recent_updated = datetime.now(UTC) - timedelta(hours=1)
        item = {
            "file_path": "/media/movies/NoAdded",
            "updated_at": recent_updated,
        }
        resolved = engine._resolve_added_at(item)
        # Falls to now (frozen), NOT to the recent updated_at marker. If
        # updated_at were still wired in, resolved would equal recent_updated.
        assert abs((resolved - datetime.now(UTC)).total_seconds()) < 2
        assert abs((resolved - recent_updated).total_seconds()) > 3000


class TestArrIdsAndTrackedFiles:
    """The scan stores TMDB/TVDB ids, tracked files and Radarr/Sonarr ids."""

    def _movie(self, tmdb_id=603):
        return {
            "plex_rating_key": "100",
            "title": "The Matrix",
            "added_at": datetime.now(UTC),
            "file_path": "/data/movies/The Matrix (1999)/matrix.mkv",
            "file_size_bytes": 5,
            "poster_path": None,
            "tmdb_id": tmdb_id,
            "tracked_files": [{"path": "/data/movies/The Matrix (1999)/matrix.mkv", "size": 5}],
        }

    def _season(self, rk, folder):
        return {
            "plex_rating_key": rk,
            "title": "Show",
            "show_title": "Show",
            "season_number": 1,
            "added_at": datetime.now(UTC),
            "file_path": folder,
            "file_size_bytes": 5,
            "poster_path": None,
            "episode_count": 1,
            "show_rating_key": "s",
            "is_anime": False,
            "tvdb_id": 121361,
            "tracked_files": [{"path": f"{folder}/S01E01.mkv", "size": 5}],
        }

    def _radarr(self):
        client = MagicMock()
        client.get_movies.return_value = [
            {"id": 7, "tmdbId": 603, "path": "/movies/The Matrix (1999)", "movieFile": None}
        ]
        return client

    def _sonarr(self, conn, seen_in_transaction):
        client = MagicMock()

        def get_series():
            seen_in_transaction.append(conn.in_transaction)
            return [
                {"id": 3, "tvdbId": 121361, "path": "/tv/Show", "seasons": [{"seasonNumber": 1}]}
            ]

        client.get_series.side_effect = get_series
        client.get_episode_files.return_value = []
        return client

    def _scan(self, conn, plex, lib_type, **clients):
        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            ScanEngine(
                conn=conn,
                plex_client=plex,
                library_ids=["1"],
                library_types={"1": lib_type},
                secret_key="k",
                **clients,
            ).run_scan()

    def _row(self, conn, rk):
        return conn.execute(
            "SELECT radarr_id, sonarr_id, tmdb_id, tracked_files FROM media_items WHERE id=?",
            (rk,),
        ).fetchone()

    def test_scan_stores_radarr_id_of_the_matching_movie(self, conn, mock_plex):
        mock_plex.get_movie_items.return_value = [self._movie()]
        radarr = self._radarr()
        self._scan(conn, mock_plex, "movie", radarr_client=radarr)
        assert self._row(conn, "100")["radarr_id"] == 7
        assert radarr.get_movies.call_count == 1

    def test_radarr_down_keeps_the_stored_radarr_id(self, conn, mock_plex):
        insert_media_item(conn, id="100", plex_rating_key="100", radarr_id=7)
        mock_plex.get_movie_items.return_value = [self._movie(tmdb_id=999)]
        radarr = MagicMock()
        radarr.get_movies.side_effect = requests.ConnectionError("down")
        self._scan(conn, mock_plex, "movie", radarr_client=radarr)
        row = self._row(conn, "100")
        assert row["radarr_id"] == 7
        assert row["tmdb_id"] == 999

    def test_library_sync_without_clients_keeps_arr_ids_and_updates_the_rest(self, conn, mock_plex):
        insert_media_item(
            conn, id="100", plex_rating_key="100", radarr_id=7, sonarr_id=3, file_path="/old"
        )
        mock_plex.get_movie_items.return_value = [self._movie()]
        ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="k",
            dry_run=True,
        ).sync_library()
        row = self._row(conn, "100")
        assert (row["radarr_id"], row["sonarr_id"]) == (7, 3)
        assert row["tmdb_id"] == 603
        assert json.loads(row["tracked_files"]) == self._movie()["tracked_files"]

    def test_season_folder_mismatch_gets_no_sonarr_id(self, conn, mock_plex):
        insert_media_item(conn, id="s2", plex_rating_key="s2", media_type="tv_season", sonarr_id=3)
        mock_plex.get_show_seasons.return_value = [
            self._season("s1", "/data/tv/Show/Season 01"),
            self._season("s2", "/data/tv-4k/Show (2011)/Season 01"),
        ]
        in_transaction: list[bool] = []
        self._scan(conn, mock_plex, "show", sonarr_client=self._sonarr(conn, in_transaction))
        assert self._row(conn, "s1")["sonarr_id"] == 3
        assert self._row(conn, "s2")["sonarr_id"] is None
        assert in_transaction == [False], "Sonarr read inside a write transaction"


class TestPostScanFollowupsAreBestEffort:
    @pytest.mark.parametrize(
        "error",
        [
            NewsletterConfigError("mailgun_base_url missing"),
            ValueError("CR/LF in from address"),
            SafeHTTPError(401, "unauthorized", "https://api.mailgun.net/v3/x"),
        ],
        ids=["config", "value", "http"],
    )
    def test_newsletter_failure_does_not_fail_the_scan(self, conn, mock_plex, error):
        with (
            patch("mediaman.scanner.engine._send_newsletter", side_effect=error),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["1"],
                library_types={"1": "movie"},
                secret_key="k",
                min_age_days=30,
                inactivity_days=30,
            )
            result = engine.run_scan()

        assert "scanned" in result

    def test_recommendation_http_failure_does_not_skip_newsletter(self, conn, mock_plex):
        with (
            patch("mediaman.scanner.engine._send_newsletter") as mock_news,
            patch(
                "mediaman.scanner.engine._refresh_recommendations",
                side_effect=SafeHTTPError(500, "boom", "https://api.openai.com/v1/x"),
            ),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["1"],
                library_types={"1": "movie"},
                secret_key="k",
                min_age_days=30,
                inactivity_days=30,
            )
            engine.run_scan()

        mock_news.assert_called_once()
