"""Tests for is_series_already_tracked and annotate_download_states (B2/M2).

Covers:
  - is_series_already_tracked: returns True when tvdbId matches, False otherwise
  - annotate_download_states: stamps download_state on each result dict
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from mediaman.services.arr.state import annotate_download_states, is_series_already_tracked


class TestIsSeriesAlreadyTracked:
    """is_series_already_tracked delegates to sonarr_client.get_series (B2)."""

    def test_returns_true_when_tvdb_id_found(self):
        client = MagicMock()
        client.get_series.return_value = [{"tvdbId": 111}, {"tvdbId": 222}]
        assert is_series_already_tracked(client, 111) is True

    def test_returns_false_when_tvdb_id_not_found(self):
        client = MagicMock()
        client.get_series.return_value = [{"tvdbId": 333}]
        assert is_series_already_tracked(client, 999) is False

    def test_returns_false_for_empty_library(self):
        client = MagicMock()
        client.get_series.return_value = []
        assert is_series_already_tracked(client, 1) is False

    def test_propagates_safe_http_error(self):
        from mediaman.services.infra import SafeHTTPError

        client = MagicMock()
        client.get_series.side_effect = SafeHTTPError(503, "Service Unavailable", b"")
        with __import__("pytest").raises(SafeHTTPError):
            is_series_already_tracked(client, 123)


class TestAnnotateDownloadStates:
    """annotate_download_states stamps download_state on result dicts (M2)."""

    def _make_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        return conn

    def test_stamps_queued_for_tracked_movie(self):
        """A movie tracked in Radarr with no file gets download_state='queued'."""
        result = {"tmdb_id": 42, "media_type": "movie", "download_state": None}
        radarr_caches = {
            "radarr_movies": {42: {"tmdbId": 42, "hasFile": False, "monitored": True}},
            "radarr_queue_tmdb_ids": set(),
        }
        sonarr_caches = {"sonarr_series": {}, "sonarr_queue_tmdb_ids": set()}

        conn = self._make_conn()
        # annotate_download_states imports build_radarr_from_db / build_sonarr_from_db
        # lazily inside the function body, so the patch must target the source module
        # (mediaman.services.arr.build) rather than the state module namespace.
        # build_radarr_cache / build_sonarr_cache are defined at module level in state.py
        # so those can be patched directly on the state module.
        with (
            patch(
                "mediaman.services.arr.build.build_radarr_from_db",
                return_value=MagicMock(),
            ),
            patch(
                "mediaman.services.arr.build.build_sonarr_from_db",
                return_value=MagicMock(),
            ),
            patch("mediaman.services.arr.state.build_radarr_cache", return_value=radarr_caches),
            patch("mediaman.services.arr.state.build_sonarr_cache", return_value=sonarr_caches),
        ):
            annotate_download_states([result], conn, "secret")

        assert result["download_state"] == "queued"

    def test_stamps_none_for_untracked_movie(self):
        """A movie not in Radarr gets download_state=None."""
        result = {"tmdb_id": 99, "media_type": "movie", "download_state": None}
        radarr_caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set()}
        sonarr_caches = {"sonarr_series": {}, "sonarr_queue_tmdb_ids": set()}

        conn = self._make_conn()
        with (
            patch("mediaman.services.arr.build.build_radarr_from_db", return_value=None),
            patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
            patch("mediaman.services.arr.state.build_radarr_cache", return_value=radarr_caches),
            patch("mediaman.services.arr.state.build_sonarr_cache", return_value=sonarr_caches),
        ):
            annotate_download_states([result], conn, "secret")

        assert result["download_state"] is None

    def test_radarr_error_leaves_state_none(self):
        """A Radarr cache build failure leaves download_state untouched (=None)."""
        import requests

        result = {"tmdb_id": 1, "media_type": "movie", "download_state": None}
        sonarr_caches = {"sonarr_series": {}, "sonarr_queue_tmdb_ids": set()}

        conn = self._make_conn()
        with (
            patch(
                "mediaman.services.arr.build.build_radarr_from_db",
                side_effect=requests.ConnectionError("down"),
            ),
            patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
            patch("mediaman.services.arr.state.build_sonarr_cache", return_value=sonarr_caches),
        ):
            annotate_download_states([result], conn, "secret")

        assert result["download_state"] is None

    def test_skips_items_without_tmdb_id(self):
        """Items with no tmdb_id are left with their original download_state."""
        result = {"tmdb_id": None, "media_type": "movie", "download_state": "sentinel"}
        radarr_caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set()}
        sonarr_caches = {"sonarr_series": {}, "sonarr_queue_tmdb_ids": set()}

        conn = self._make_conn()
        with (
            patch("mediaman.services.arr.build.build_radarr_from_db", return_value=None),
            patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
            patch("mediaman.services.arr.state.build_radarr_cache", return_value=radarr_caches),
            patch("mediaman.services.arr.state.build_sonarr_cache", return_value=sonarr_caches),
        ):
            annotate_download_states([result], conn, "secret")

        assert result["download_state"] == "sentinel"
