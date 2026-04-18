"""Tests for the Radarr/Sonarr download-state helper."""
from mediaman.services.arr_state import compute_download_state


def _movie_caches(in_library=False, in_queue=False, tmdb_id=100):
    movie = {"tmdbId": tmdb_id, "hasFile": in_library}
    return {
        "radarr_movies": {tmdb_id: movie},
        "radarr_queue_tmdb_ids": {tmdb_id} if in_queue else set(),
        "sonarr_series": {},
        "sonarr_queue_tmdb_ids": set(),
    }


def _series(tmdb_id, seasons):
    """seasons: list of (season_number, episode_count, episode_file_count, aired)"""
    return {
        "tmdbId": tmdb_id,
        "statistics": {"seasonCount": len(seasons)},
        "seasons": [
            {
                "seasonNumber": n,
                "monitored": True,
                "statistics": {
                    "episodeCount": total,
                    "episodeFileCount": files,
                    "previousAiring": "2020-01-01" if aired else None,
                },
            }
            for (n, total, files, aired) in seasons
        ],
    }


class TestComputeDownloadState:
    def test_movie_not_in_radarr_returns_null(self):
        caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(),
                  "sonarr_series": {}, "sonarr_queue_tmdb_ids": set()}
        assert compute_download_state("movie", 100, caches) is None

    def test_movie_with_file_returns_in_library(self):
        assert compute_download_state("movie", 100, _movie_caches(in_library=True)) == "in_library"

    def test_movie_in_queue_returns_downloading(self):
        caches = _movie_caches(in_library=False, in_queue=True)
        assert compute_download_state("movie", 100, caches) == "downloading"

    def test_movie_tracked_no_file_no_queue_returns_queued(self):
        assert compute_download_state("movie", 100, _movie_caches()) == "queued"

    def test_tv_not_tracked_returns_null(self):
        caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(),
                  "sonarr_series": {}, "sonarr_queue_tmdb_ids": set()}
        assert compute_download_state("tv", 500, caches) is None

    def test_tv_all_aired_seasons_downloaded_returns_in_library(self):
        series = _series(500, [(1, 10, 10, True), (2, 8, 8, True)])
        caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(),
                  "sonarr_series": {500: series}, "sonarr_queue_tmdb_ids": set()}
        assert compute_download_state("tv", 500, caches) == "in_library"

    def test_tv_some_aired_seasons_missing_returns_partial(self):
        series = _series(500, [(1, 10, 10, True), (2, 8, 0, True)])
        caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(),
                  "sonarr_series": {500: series}, "sonarr_queue_tmdb_ids": set()}
        assert compute_download_state("tv", 500, caches) == "partial"

    def test_tv_partial_ignores_unaired_seasons(self):
        # S1 fully downloaded, S2 not yet aired → still in_library
        series = _series(500, [(1, 10, 10, True), (2, 0, 0, False)])
        caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(),
                  "sonarr_series": {500: series}, "sonarr_queue_tmdb_ids": set()}
        assert compute_download_state("tv", 500, caches) == "in_library"

    def test_tv_in_queue_returns_downloading(self):
        caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(),
                  "sonarr_series": {500: _series(500, [(1, 10, 0, True)])},
                  "sonarr_queue_tmdb_ids": {500}}
        assert compute_download_state("tv", 500, caches) == "downloading"

    def test_tv_tracked_no_files_no_queue_returns_queued(self):
        series = _series(500, [(1, 10, 0, True)])
        caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(),
                  "sonarr_series": {500: series}, "sonarr_queue_tmdb_ids": set()}
        assert compute_download_state("tv", 500, caches) == "queued"

    def test_tv_aired_season_with_zero_episode_count_does_not_mask_partial(self):
        # S1 fully downloaded, S2 aired but Sonarr reports 0 episodes known
        # yet (can happen right after a season announcement). Must not
        # silently satisfy have_all via 0 >= 0.
        series = _series(500, [(1, 10, 10, True), (2, 0, 0, True)])
        caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(),
                  "sonarr_series": {500: series}, "sonarr_queue_tmdb_ids": set()}
        assert compute_download_state("tv", 500, caches) == "partial"


def test_tv_season_without_statistics_key_is_skipped():
    # Raw season dict with no `statistics` key — defensive reads must
    # not raise and must treat the season as unaired.
    series = {
        "tmdbId": 600,
        "seasons": [{"seasonNumber": 1}],
    }
    caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(),
              "sonarr_series": {600: series}, "sonarr_queue_tmdb_ids": set()}
    # No aired_seasons → falls through to queue/queued logic.
    assert compute_download_state("tv", 600, caches) == "queued"


# Tests for cache builders
from unittest.mock import MagicMock
from mediaman.services.arr_state import build_radarr_cache, build_sonarr_cache


def test_build_radarr_cache_indexes_by_tmdb_id():
    radarr = MagicMock()
    radarr.get_movies.return_value = [{"tmdbId": 1, "title": "A"}, {"tmdbId": 2, "title": "B"}]
    radarr.get_queue.return_value = [{"movie": {"tmdbId": 2}}]
    cache = build_radarr_cache(radarr)
    assert set(cache["radarr_movies"].keys()) == {1, 2}
    assert cache["radarr_queue_tmdb_ids"] == {2}


def test_build_radarr_cache_handles_none_client():
    cache = build_radarr_cache(None)
    assert cache == {"radarr_movies": {}, "radarr_queue_tmdb_ids": set()}


def test_build_sonarr_cache_indexes_by_tmdb_id():
    sonarr = MagicMock()
    sonarr.get_series.return_value = [{"tmdbId": 10, "title": "X"}, {"tmdbId": 20, "title": "Y"}]
    sonarr.get_queue.return_value = [{"series": {"tmdbId": 20}}]
    cache = build_sonarr_cache(sonarr)
    assert set(cache["sonarr_series"].keys()) == {10, 20}
    assert cache["sonarr_queue_tmdb_ids"] == {20}


def test_build_sonarr_cache_handles_none_client():
    cache = build_sonarr_cache(None)
    assert cache == {"sonarr_series": {}, "sonarr_queue_tmdb_ids": set()}


def test_build_radarr_cache_filters_null_tmdb_ids_and_handles_none_queue_movie():
    radarr = MagicMock()
    radarr.get_movies.return_value = [
        {"tmdbId": None, "title": "no id"},
        {"title": "missing tmdb key"},
        {"tmdbId": 5, "title": "keep"},
    ]
    radarr.get_queue.return_value = [
        {"movie": None},
        {"movie": {"tmdbId": None}},
        {"movie": {"tmdbId": 5}},
    ]
    cache = build_radarr_cache(radarr)
    assert set(cache["radarr_movies"].keys()) == {5}
    assert cache["radarr_queue_tmdb_ids"] == {5}


def test_build_sonarr_cache_filters_null_tmdb_ids_and_handles_none_queue_series():
    sonarr = MagicMock()
    sonarr.get_series.return_value = [
        {"tmdbId": None, "title": "no id"},
        {"title": "missing tmdb key"},
        {"tmdbId": 7, "title": "keep"},
    ]
    sonarr.get_queue.return_value = [
        {"series": None},
        {"series": {"tmdbId": None}},
        {"series": {"tmdbId": 7}},
    ]
    cache = build_sonarr_cache(sonarr)
    assert set(cache["sonarr_series"].keys()) == {7}
    assert cache["sonarr_queue_tmdb_ids"] == {7}
