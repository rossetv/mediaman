"""Tests for the Radarr/Sonarr download-state helper."""

from __future__ import annotations

from mediaman.services.arr.state import compute_download_state


def _movie_caches(in_library=False, in_queue=False, tmdb_id=100, monitored=True):
    movie = {"tmdbId": tmdb_id, "hasFile": in_library, "monitored": monitored}
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
        caches = {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {},
            "sonarr_queue_tmdb_ids": set(),
        }
        assert compute_download_state("movie", 100, caches) is None

    def test_movie_with_file_returns_in_library(self):
        assert compute_download_state("movie", 100, _movie_caches(in_library=True)) == "in_library"

    def test_movie_in_queue_returns_downloading(self):
        caches = _movie_caches(in_library=False, in_queue=True)
        assert compute_download_state("movie", 100, caches) == "downloading"

    def test_movie_tracked_no_file_no_queue_returns_queued(self):
        assert compute_download_state("movie", 100, _movie_caches()) == "queued"

    def test_movie_unmonitored_no_file_returns_null(self):
        """An abandoned (unmonitored) movie should report as untracked.

        Regression: after auto-abandon (or manual abandon) the movie stays
        in Radarr but ``monitored=False``. Reporting it as ``queued``
        wedged the search modal — the button rendered as a disabled
        "Queued" pill, leaving no way to re-download. Treating it as
        untracked here lets the search/download endpoint re-monitor on
        click.
        """
        caches = _movie_caches(monitored=False)
        assert compute_download_state("movie", 100, caches) is None

    def test_movie_unmonitored_in_library_still_in_library(self):
        """An unmonitored movie that already has a file is still ``in_library``.

        ``hasFile`` wins over ``monitored=False`` — the user has the
        copy; the abandon-residue heuristic only matters for entries
        with no file.
        """
        caches = _movie_caches(in_library=True, monitored=False)
        assert compute_download_state("movie", 100, caches) == "in_library"

    def test_tv_not_tracked_returns_null(self):
        caches = {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {},
            "sonarr_queue_tmdb_ids": set(),
        }
        assert compute_download_state("tv", 500, caches) is None

    def test_tv_all_aired_seasons_downloaded_returns_in_library(self):
        series = _series(500, [(1, 10, 10, True), (2, 8, 8, True)])
        caches = {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {500: series},
            "sonarr_queue_tmdb_ids": set(),
        }
        assert compute_download_state("tv", 500, caches) == "in_library"

    def test_tv_some_aired_seasons_missing_returns_partial(self):
        series = _series(500, [(1, 10, 10, True), (2, 8, 0, True)])
        caches = {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {500: series},
            "sonarr_queue_tmdb_ids": set(),
        }
        assert compute_download_state("tv", 500, caches) == "partial"

    def test_tv_partial_ignores_unaired_seasons(self):
        # S1 fully downloaded, S2 not yet aired → still in_library
        series = _series(500, [(1, 10, 10, True), (2, 0, 0, False)])
        caches = {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {500: series},
            "sonarr_queue_tmdb_ids": set(),
        }
        assert compute_download_state("tv", 500, caches) == "in_library"

    def test_tv_in_queue_returns_downloading(self):
        caches = {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {500: _series(500, [(1, 10, 0, True)])},
            "sonarr_queue_tmdb_ids": {500},
        }
        assert compute_download_state("tv", 500, caches) == "downloading"

    def test_tv_tracked_no_files_no_queue_returns_queued(self):
        series = _series(500, [(1, 10, 0, True)])
        caches = {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {500: series},
            "sonarr_queue_tmdb_ids": set(),
        }
        assert compute_download_state("tv", 500, caches) == "queued"

    def test_tv_aired_season_with_zero_episode_count_does_not_mask_partial(self):
        # S1 fully downloaded, S2 aired but Sonarr reports 0 episodes known
        # yet (can happen right after a season announcement). Must not
        # silently satisfy have_all via 0 >= 0.
        series = _series(500, [(1, 10, 10, True), (2, 0, 0, True)])
        caches = {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {500: series},
            "sonarr_queue_tmdb_ids": set(),
        }
        assert compute_download_state("tv", 500, caches) == "partial"


def test_tv_season_without_statistics_key_is_skipped():
    # Raw season dict with no `statistics` key — defensive reads must
    # not raise and must treat the season as unaired.
    series = {
        "tmdbId": 600,
        "seasons": [{"seasonNumber": 1}],
    }
    caches = {
        "radarr_movies": {},
        "radarr_queue_tmdb_ids": set(),
        "sonarr_series": {600: series},
        "sonarr_queue_tmdb_ids": set(),
    }
    # No aired_seasons → falls through to queue/queued logic.
    assert compute_download_state("tv", 600, caches) == "queued"


# Tests for cache builders
from unittest.mock import MagicMock  # noqa: E402 — grouped after test classes intentionally

from mediaman.services.arr.state import build_radarr_cache, build_sonarr_cache  # noqa: E402


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


def test_build_radarr_cache_logs_warning_on_duplicate_tmdb_id(caplog):
    """Two movies sharing a tmdbId is suspicious — the cache logs a warning."""
    radarr = MagicMock()
    radarr.get_movies.return_value = [
        {"tmdbId": 99, "title": "Original"},
        {"tmdbId": 99, "title": "Duplicate"},
    ]
    radarr.get_queue.return_value = []
    with caplog.at_level("WARNING", logger="mediaman"):
        cache = build_radarr_cache(radarr)
    # Last write wins (matches dict-update semantics).
    assert cache["radarr_movies"][99]["title"] == "Duplicate"
    assert any("duplicate tmdbId=99" in r.message for r in caplog.records)


def test_build_sonarr_cache_logs_warning_on_duplicate_tmdb_id(caplog):
    """Two series sharing a tmdbId is suspicious — the cache logs a warning."""
    sonarr = MagicMock()
    sonarr.get_series.return_value = [
        {"tmdbId": 50, "title": "Original"},
        {"tmdbId": 50, "title": "Duplicate"},
    ]
    sonarr.get_queue.return_value = []
    with caplog.at_level("WARNING", logger="mediaman"):
        cache = build_sonarr_cache(sonarr)
    assert cache["sonarr_series"][50]["title"] == "Duplicate"
    assert any("duplicate tmdbId=50" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# previousAiring / previousAiringDate fallback
# ---------------------------------------------------------------------------


def test_tv_previous_airing_date_treated_as_aired_signal():
    """Older Sonarr exposes ``previousAiringDate`` instead of ``previousAiring``.

    The state computation now accepts either field so a freshly-upgraded
    Sonarr (or a downgrade) doesn't silently report every season as
    unaired and collapse to ``queued``.
    """
    series = {
        "tmdbId": 700,
        "seasons": [
            {
                "seasonNumber": 1,
                "monitored": True,
                "statistics": {
                    "episodeCount": 10,
                    "episodeFileCount": 10,
                    # No previousAiring; older field present instead.
                    "previousAiringDate": "2020-01-01",
                },
            }
        ],
    }
    caches = {
        "radarr_movies": {},
        "radarr_queue_tmdb_ids": set(),
        "sonarr_series": {700: series},
        "sonarr_queue_tmdb_ids": set(),
    }
    assert compute_download_state("tv", 700, caches) == "in_library"


# ---------------------------------------------------------------------------
# _season_stats / _season_has_aired — pure helpers promoted out of
# compute_download_state's series branch (Phase-4 decomposition)
# ---------------------------------------------------------------------------

from mediaman.services.arr.state import _season_has_aired, _season_stats  # noqa: E402


class TestSeasonStats:
    def test_returns_statistics_dict_when_present(self):
        season = {"seasonNumber": 1, "statistics": {"episodeFileCount": 3}}
        assert _season_stats(season) == {"episodeFileCount": 3}

    def test_returns_empty_dict_when_statistics_absent(self):
        assert _season_stats({"seasonNumber": 1}) == {}

    def test_returns_empty_dict_when_statistics_malformed(self):
        # A non-dict ``statistics`` (e.g. None or a list) must not raise.
        assert _season_stats({"statistics": None}) == {}
        assert _season_stats({"statistics": []}) == {}


class TestSeasonHasAired:
    def test_previous_airing_on_statistics_signals_aired(self):
        season = {"statistics": {"previousAiring": "2020-01-01"}}
        assert _season_has_aired(season) is True

    def test_legacy_previous_airing_date_on_season_signals_aired(self):
        # Older Sonarr exposed ``previousAiringDate`` on the season payload.
        assert _season_has_aired({"previousAiringDate": "2019-05-05"}) is True

    def test_legacy_previous_airing_date_on_statistics_signals_aired(self):
        season = {"statistics": {"previousAiringDate": "2019-05-05"}}
        assert _season_has_aired(season) is True

    def test_no_airing_signal_is_unaired(self):
        assert _season_has_aired({"statistics": {"episodeCount": 10}}) is False
        assert _season_has_aired({"seasonNumber": 1}) is False


# ---------------------------------------------------------------------------
# attach_download_states — per-item Arr enrichment lifted out of the
# /recommended page handler
# ---------------------------------------------------------------------------

from mediaman.services.arr.state import attach_download_states  # noqa: E402


class _FakeArr:
    """Stand-in for ``LazyArrClients`` that records whether each client
    was actually requested, so a test can assert the lazy-build /
    build-empty-half-once behaviour."""

    def __init__(self, radarr=None, sonarr=None):
        self._radarr = radarr
        self._sonarr = sonarr
        self.radarr_calls = 0
        self.sonarr_calls = 0

    def radarr(self):
        self.radarr_calls += 1
        return self._radarr

    def sonarr(self):
        self.sonarr_calls += 1
        return self._sonarr


def _batch(*items):
    """Build a formatted batch dict, splitting items into trending/personal
    by their ``category`` key (defaulting to personal)."""
    trending = [i for i in items if i.get("category") == "trending"]
    personal = [i for i in items if i.get("category") != "trending"]
    return {"trending": trending, "personal": personal}


def test_attach_download_states_mutates_items_in_place_and_returns_map():
    radarr = MagicMock()
    radarr.get_movies.return_value = [{"tmdbId": 100, "hasFile": True, "monitored": True}]
    radarr.get_queue.return_value = []
    arr = _FakeArr(radarr=radarr)

    movie = {"id": "s1", "tmdb_id": 100, "media_type": "movie"}
    batches = [_batch(movie)]
    all_recs = attach_download_states(batches, arr)

    # The item dict is mutated in place …
    assert movie["download_state"] == "in_library"
    # … and the same object is returned keyed by its id.
    assert all_recs == {"s1": movie}
    assert all_recs["s1"] is movie


def test_attach_download_states_only_builds_clients_that_are_needed():
    """A batch with only movie items must not build the Sonarr client,
    and vice versa — the empty opposite-half is reused, not fetched."""
    radarr = MagicMock()
    radarr.get_movies.return_value = []
    radarr.get_queue.return_value = []
    arr = _FakeArr(radarr=radarr, sonarr=MagicMock())

    batches = [_batch({"id": "m", "tmdb_id": 1, "media_type": "movie"})]
    attach_download_states(batches, arr)

    assert arr.radarr_calls == 1  # built once for the movie item
    assert arr.sonarr_calls == 0  # never built — no TV item present


def test_attach_download_states_builds_each_client_at_most_once():
    radarr = MagicMock()
    radarr.get_movies.return_value = []
    radarr.get_queue.return_value = []
    sonarr = MagicMock()
    sonarr.get_series.return_value = []
    sonarr.get_queue.return_value = []
    arr = _FakeArr(radarr=radarr, sonarr=sonarr)

    # Two movie items and two TV items spread across batches.
    batches = [
        _batch(
            {"id": "m1", "tmdb_id": 1, "media_type": "movie"},
            {"id": "t1", "tmdb_id": 2, "media_type": "tv"},
        ),
        _batch(
            {"id": "m2", "tmdb_id": 3, "media_type": "movie"},
            {"id": "t2", "tmdb_id": 4, "media_type": "tv"},
        ),
    ]
    all_recs = attach_download_states(batches, arr)

    assert arr.radarr_calls == 1
    assert arr.sonarr_calls == 1
    assert set(all_recs) == {"m1", "t1", "m2", "t2"}


def test_attach_download_states_keeps_untracked_items_without_state():
    """An item with no ``tmdb_id`` is still collected into the returned
    map but never gets a ``download_state`` written."""
    radarr = MagicMock()
    radarr.get_movies.return_value = []
    radarr.get_queue.return_value = []
    arr = _FakeArr(radarr=radarr, sonarr=MagicMock())

    no_tmdb = {"id": "x", "media_type": "movie"}
    untracked = {"id": "y", "tmdb_id": 999, "media_type": "movie"}
    batches = [_batch(no_tmdb, untracked)]
    all_recs = attach_download_states(batches, arr)

    assert "download_state" not in no_tmdb
    # tmdb_id present but not in Radarr → compute returns None → no write.
    assert "download_state" not in untracked
    assert set(all_recs) == {"x", "y"}
    # The movie client was still needed (untracked item has a tmdb_id).
    assert arr.radarr_calls == 1


def test_tv_unmonitored_aired_season_is_ignored():
    """An unmonitored aired season the user explicitly skipped doesn't drag the show into ``partial``."""
    series = {
        "tmdbId": 800,
        "seasons": [
            {
                "seasonNumber": 1,
                "monitored": True,
                "statistics": {
                    "episodeCount": 10,
                    "episodeFileCount": 10,
                    "previousAiring": "2020-01-01",
                },
            },
            {
                "seasonNumber": 2,
                # User explicitly unmonitored this season; it has aired but
                # no files — without the monitored filter the show would
                # report ``partial``.
                "monitored": False,
                "statistics": {
                    "episodeCount": 8,
                    "episodeFileCount": 0,
                    "previousAiring": "2021-01-01",
                },
            },
        ],
    }
    caches = {
        "radarr_movies": {},
        "radarr_queue_tmdb_ids": set(),
        "sonarr_series": {800: series},
        "sonarr_queue_tmdb_ids": set(),
    }
    assert compute_download_state("tv", 800, caches) == "in_library"
