"""Tests for mediaman.scanner.arr_dates.

Covers: normalise_path, ArrDateCache (lazy loading, Radarr/Sonarr build,
error tolerance).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import requests

from mediaman.scanner.arr_dates import ArrDateCache, normalise_path

# ---------------------------------------------------------------------------
# normalise_path
# ---------------------------------------------------------------------------


class TestNormalisePath:
    def test_strips_data_prefix(self):
        assert normalise_path("/data/movies/Film (2020)/Film.mkv") == "movies/Film (2020)/Film.mkv"

    def test_strips_media_prefix(self):
        assert normalise_path("/media/movies/Film.mkv") == "movies/Film.mkv"

    def test_strips_share_prefix(self):
        assert normalise_path("/share/tv/Show/S01") == "tv/Show/S01"

    def test_keeps_non_generic_first_component(self):
        # "/movies/Film.mkv" → "movies/Film.mkv" (first component is not
        # a generic root like "data", so just strip leading slash).
        result = normalise_path("/movies/Film (2020)/Film.mkv")
        assert result == "movies/Film (2020)/Film.mkv"

    def test_single_component_returned_as_is(self):
        # Too short to split — must not crash.
        assert normalise_path("/Film.mkv") == "/Film.mkv"

    def test_empty_string(self):
        assert normalise_path("") == ""


# ---------------------------------------------------------------------------
# ArrDateCache
# ---------------------------------------------------------------------------


class TestArrDateCacheLazyLoad:
    def test_not_loaded_until_get_called(self):
        cache = ArrDateCache()
        assert cache._loaded is False

    def test_loaded_after_get(self):
        cache = ArrDateCache()
        cache.get("/nonexistent/path")
        assert cache._loaded is True

    def test_loaded_after_ensure_loaded(self):
        cache = ArrDateCache()
        cache.ensure_loaded()
        assert cache._loaded is True

    def test_build_called_only_once(self):
        cache = ArrDateCache()
        cache.ensure_loaded()
        # Manually mark something in the dict to confirm _build only ran once.
        cache._dates["marker"] = "2026-01-01"
        cache.ensure_loaded()  # second call — must not clear the dict.
        assert "marker" in cache._dates


class TestArrDateCacheRadarr:
    def _make_radarr(self, movies):
        client = MagicMock()
        client.get_movies.return_value = movies
        return client

    def test_indexes_radarr_movie_file_path(self):
        radarr = self._make_radarr(
            [
                {
                    "movieFile": {
                        "path": "/data/movies/Film (2020)/Film.mkv",
                        "dateAdded": "2024-01-15T10:00:00Z",
                    }
                }
            ]
        )
        cache = ArrDateCache(radarr_client=radarr)
        result = cache.get("/movies/Film (2020)/Film.mkv")
        assert result == "2024-01-15T10:00:00Z"

    def test_skips_movie_with_no_file(self):
        radarr = self._make_radarr([{"movieFile": None}])
        cache = ArrDateCache(radarr_client=radarr)
        cache.ensure_loaded()
        assert cache._dates == {}

    def test_radarr_exception_does_not_raise(self):
        radarr = MagicMock()
        radarr.get_movies.side_effect = requests.ConnectionError("network error")
        cache = ArrDateCache(radarr_client=radarr)
        # Must not propagate — logs a warning and falls back to empty.
        cache.ensure_loaded()
        assert cache._dates == {}

    def test_returns_none_for_unknown_path(self):
        cache = ArrDateCache()
        assert cache.get("/movies/Unknown/Unknown.mkv") is None


class TestArrDateCacheSonarr:
    def _make_sonarr(self, series, episode_files_by_id):
        client = MagicMock()
        client.get_series.return_value = series

        def get_episode_files(series_id):
            return episode_files_by_id.get(series_id, [])

        client.get_episode_files.side_effect = get_episode_files
        return client

    def test_indexes_sonarr_season_directory(self):
        sonarr = self._make_sonarr(
            series=[{"id": 1}],
            episode_files_by_id={
                1: [
                    {
                        "path": "/data/tv/Show/Season 01/S01E01.mkv",
                        "dateAdded": "2024-03-01T00:00:00Z",
                    }
                ]
            },
        )
        cache = ArrDateCache(sonarr_client=sonarr)
        # Season dir is "/data/tv/Show/Season 01" → normalised to "tv/Show/Season 01"
        result = cache.get("/tv/Show/Season 01")
        assert result == "2024-03-01T00:00:00Z"

    def test_sonarr_keeps_most_recent_date_per_season(self):
        sonarr = self._make_sonarr(
            series=[{"id": 1}],
            episode_files_by_id={
                1: [
                    {"path": "/tv/S1/S01E01.mkv", "dateAdded": "2024-01-01T00:00:00Z"},
                    {"path": "/tv/S1/S01E02.mkv", "dateAdded": "2024-06-01T00:00:00Z"},
                ]
            },
        )
        cache = ArrDateCache(sonarr_client=sonarr)
        result = cache.get("/tv/S1")
        assert result == "2024-06-01T00:00:00Z"

    def test_sonarr_exception_does_not_raise(self):
        sonarr = MagicMock()
        sonarr.get_series.side_effect = requests.ConnectionError("API down")
        cache = ArrDateCache(sonarr_client=sonarr)
        cache.ensure_loaded()  # must not propagate
        assert cache._dates == {}

    def test_sonarr_compares_z_and_offset_forms_correctly(self):
        """Domain 05: comparing ISO strings as plain strings was wrong.

        ``"2024-06-01T00:00:00Z"`` (UTC) and
        ``"2024-06-01T00:00:00+00:00"`` (offset form) describe the same
        instant but compare unequal as strings — ``"+"`` (0x2B) sorts
        before ``"Z"`` (0x5A) in ASCII, so a later episode using the
        offset form would silently lose to an older Z-form date and
        the season's "latest download" would be wrong.
        """
        sonarr = self._make_sonarr(
            series=[{"id": 1}],
            episode_files_by_id={
                1: [
                    # The first file uses the Z form; the second is later in
                    # absolute time but written in offset form. As plain
                    # strings the second compares LESS than the first, so
                    # the old code would have kept the older Z-form date.
                    {"path": "/tv/S1/S01E01.mkv", "dateAdded": "2024-01-01T00:00:00Z"},
                    {"path": "/tv/S1/S01E02.mkv", "dateAdded": "2024-06-01T00:00:00+00:00"},
                ]
            },
        )
        cache = ArrDateCache(sonarr_client=sonarr)
        result = cache.get("/tv/S1")
        # The newer date (June) must win regardless of the suffix form.
        assert result == "2024-06-01T00:00:00+00:00"

    def test_sonarr_skips_unparseable_date(self):
        """An unparseable ``dateAdded`` must not corrupt the cached value."""
        sonarr = self._make_sonarr(
            series=[{"id": 1}],
            episode_files_by_id={
                1: [
                    {"path": "/tv/S1/S01E01.mkv", "dateAdded": "2024-01-01T00:00:00Z"},
                    {"path": "/tv/S1/S01E02.mkv", "dateAdded": "not a date"},
                ]
            },
        )
        cache = ArrDateCache(sonarr_client=sonarr)
        result = cache.get("/tv/S1")
        # The garbage date is skipped; the parseable one remains cached.
        assert result == "2024-01-01T00:00:00Z"
