"""Tests for the Radarr/Sonarr download-state helper."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mediaman.services.arr.base import SearchUnanswered
from mediaman.services.arr.state import (
    ExistingMovieError,
    add_or_remonitor_movie,
    compute_download_state,
)
from mediaman.services.infra import SafeHTTPError


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

from mediaman.services.arr.state import build_radarr_cache, build_sonarr_cache  # noqa: E402


def test_radarr_cache_keyed_by_tmdb_id():
    radarr = MagicMock()
    radarr.get_movies.return_value = [{"tmdbId": 1, "title": "A"}, {"tmdbId": 2, "title": "B"}]
    radarr.get_queue.return_value = [{"movie": {"tmdbId": 2}}]
    cache = build_radarr_cache(radarr)
    assert set(cache["radarr_movies"].keys()) == {1, 2}
    assert cache["radarr_queue_tmdb_ids"] == {2}


def test_build_radarr_cache_handles_none_client():
    cache = build_radarr_cache(None)
    assert cache == {
        "radarr_movies": {},
        "radarr_queue_tmdb_ids": set(),
        "radarr_queue_progress": {},
    }


def test_sonarr_cache_keyed_by_tmdb_id():
    sonarr = MagicMock()
    sonarr.get_series.return_value = [{"tmdbId": 10, "title": "X"}, {"tmdbId": 20, "title": "Y"}]
    sonarr.get_queue.return_value = [{"series": {"tmdbId": 20}}]
    cache = build_sonarr_cache(sonarr)
    assert set(cache["sonarr_series"].keys()) == {10, 20}
    assert cache["sonarr_queue_tmdb_ids"] == {20}


def test_build_sonarr_cache_handles_none_client():
    cache = build_sonarr_cache(None)
    assert cache == {
        "sonarr_series": {},
        "sonarr_queue_tmdb_ids": set(),
        "sonarr_queue_progress": {},
    }


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


from mediaman.services.arr.state import _season_has_aired, _season_stats  # noqa: E402


class TestSeasonStats:
    def test_statistics_dict_is_returned_when_present(self):
        season = {"seasonNumber": 1, "statistics": {"episodeFileCount": 3}}
        assert _season_stats(season) == {"episodeFileCount": 3}

    def test_empty_dict_returned_when_statistics_key_absent(self):
        assert _season_stats({"seasonNumber": 1}) == {}

    def test_empty_dict_returned_when_statistics_is_not_a_dict(self):
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


def test_attach_skips_and_logs_unrecognised_media_type(caplog):
    """An item with a usable tmdb_id but a media_type that isn't
    "movie"/"tv" must be skipped+logged, not silently classed as a series.

    Regression: ``compute_download_state`` treats anything that isn't
    "movie" as a Sonarr series, so a stray value ("anime", "tv " with
    whitespace) would be looked up in the sonarr cache and mislabelled.
    """
    import logging

    radarr = MagicMock()
    radarr.get_movies.return_value = []
    radarr.get_queue.return_value = []
    sonarr = MagicMock()
    sonarr.get_series.return_value = []
    sonarr.get_queue.return_value = []
    arr = _FakeArr(radarr=radarr, sonarr=sonarr)

    bad = {"id": "z", "tmdb_id": 555, "media_type": "anime"}
    batches = [_batch(bad)]
    with caplog.at_level(logging.WARNING):
        all_recs = attach_download_states(batches, arr)

    # No misclassification: never written a download_state, never built the
    # sonarr client to look it up as a series.
    assert "download_state" not in bad
    assert arr.sonarr_calls == 0
    assert set(all_recs) == {"z"}
    assert any("unrecognised media_type" in r.message for r in caplog.records)


def test_attach_clears_stale_downloaded_at_when_arr_configured_and_absent():
    """A ``downloaded_at`` flag on an item Radarr no longer tracks is stale.

    Regression: the card/modal fall back to a "Queued" badge off
    ``downloaded_at`` whenever there's no live state, so an item downloaded
    then deleted showed a phantom "Queued" forever. With Radarr reachable
    and the item absent, the flag is cleared in memory.
    """
    radarr = MagicMock()
    radarr.get_movies.return_value = []  # configured, but tmdb 100 not tracked
    radarr.get_queue.return_value = []
    arr = _FakeArr(radarr=radarr)

    movie = {
        "id": "s1",
        "tmdb_id": 100,
        "media_type": "movie",
        "downloaded_at": "2026-01-01T00:00:00+00:00",
    }
    attach_download_states([_batch(movie)], arr)

    assert "download_state" not in movie
    assert movie["downloaded_at"] is None


def test_attach_keeps_downloaded_at_when_arr_unconfigured():
    """When the Arr client is not configured we can't confirm absence, so a
    fresh optimistic ``downloaded_at`` must be left intact."""
    arr = _FakeArr(radarr=None)  # unconfigured

    movie = {
        "id": "s1",
        "tmdb_id": 100,
        "media_type": "movie",
        "downloaded_at": "2026-01-01T00:00:00+00:00",
    }
    attach_download_states([_batch(movie)], arr)

    assert movie["downloaded_at"] == "2026-01-01T00:00:00+00:00"


def test_attach_keeps_downloaded_at_when_item_still_tracked():
    """An item still in Radarr keeps its flag (live state drives the badge)."""
    radarr = MagicMock()
    radarr.get_movies.return_value = [{"tmdbId": 100, "hasFile": False, "monitored": True}]
    radarr.get_queue.return_value = []
    arr = _FakeArr(radarr=radarr)

    movie = {
        "id": "s1",
        "tmdb_id": 100,
        "media_type": "movie",
        "downloaded_at": "2026-01-01T00:00:00+00:00",
    }
    attach_download_states([_batch(movie)], arr)

    assert movie["download_state"] == "queued"
    assert movie["downloaded_at"] == "2026-01-01T00:00:00+00:00"


def test_attach_clears_stale_downloaded_at_in_db(db_path):
    """The stale flag is also cleared in the suggestions table via *conn*."""
    from mediaman.db import init_db

    conn = init_db(str(db_path))
    conn.execute(
        "INSERT INTO suggestions (id, title, media_type, category, tmdb_id, "
        "downloaded_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "Arrival", "movie", "personal", 100, "2026-01-01T00:00:00+00:00", "2026-01-01"),
    )
    conn.commit()

    radarr = MagicMock()
    radarr.get_movies.return_value = []  # configured but item gone
    radarr.get_queue.return_value = []
    arr = _FakeArr(radarr=radarr)

    item = {
        "id": 1,
        "tmdb_id": 100,
        "media_type": "movie",
        "downloaded_at": "2026-01-01T00:00:00+00:00",
    }
    attach_download_states([_batch(item)], arr, conn)

    row = conn.execute("SELECT downloaded_at FROM suggestions WHERE id = 1").fetchone()
    assert row[0] is None


def test_attach_renders_without_sonarr_state_when_get_series_raises_safe_http_error():
    """A SafeHTTPError from Sonarr must not propagate out of
    attach_download_states — the recommendations are returned with the TV
    item left without download state (graceful degradation, no 500)."""
    import pytest

    from mediaman.services.infra import SafeHTTPError

    sonarr = MagicMock()
    sonarr.get_series.side_effect = SafeHTTPError(
        503, "Sonarr unreachable", "http://sonarr/api/v3/series"
    )
    arr = _FakeArr(sonarr=sonarr)

    tv = {"id": "t1", "tmdb_id": 200, "media_type": "tv"}
    try:
        all_recs = attach_download_states([_batch(tv)], arr)
    except SafeHTTPError:  # pragma: no cover - asserts the bug is fixed
        pytest.fail("SafeHTTPError leaked out of attach_download_states")

    assert "download_state" not in tv
    assert set(all_recs) == {"t1"}


def test_attach_renders_without_radarr_state_when_get_movies_raises_safe_http_error():
    """A SafeHTTPError from Radarr must not propagate — the movie item is
    returned without download state instead of 500ing the page."""
    import pytest

    from mediaman.services.infra import SafeHTTPError

    radarr = MagicMock()
    radarr.get_movies.side_effect = SafeHTTPError(
        503, "Radarr unreachable", "http://radarr/api/v3/movie"
    )
    arr = _FakeArr(radarr=radarr)

    movie = {"id": "m1", "tmdb_id": 100, "media_type": "movie"}
    try:
        all_recs = attach_download_states([_batch(movie)], arr)
    except SafeHTTPError:  # pragma: no cover
        pytest.fail("SafeHTTPError leaked out of attach_download_states")

    assert "download_state" not in movie
    assert set(all_recs) == {"m1"}


def test_attach_renders_without_state_when_cache_build_raises_arr_error():
    """A domain ArrError (e.g. ArrUpstreamError) is also caught and degraded."""
    import pytest

    from mediaman.services.arr import ArrUpstreamError

    radarr = MagicMock()
    radarr.get_movies.side_effect = ArrUpstreamError("null body where a record was expected")
    arr = _FakeArr(radarr=radarr)

    movie = {"id": "m1", "tmdb_id": 100, "media_type": "movie"}
    try:
        all_recs = attach_download_states([_batch(movie)], arr)
    except ArrUpstreamError:  # pragma: no cover
        pytest.fail("ArrError leaked out of attach_download_states")

    assert "download_state" not in movie
    assert set(all_recs) == {"m1"}


def test_attach_degrades_each_arr_service_independently():
    """Sonarr being down must not hide Radarr download state, and vice versa:
    a failing Sonarr leaves the movie's Radarr state intact."""
    from mediaman.services.infra import SafeHTTPError

    radarr = MagicMock()
    radarr.get_movies.return_value = [{"tmdbId": 100, "hasFile": True, "monitored": True}]
    radarr.get_queue.return_value = []
    sonarr = MagicMock()
    sonarr.get_series.side_effect = SafeHTTPError(
        503, "Sonarr unreachable", "http://sonarr/api/v3/series"
    )
    arr = _FakeArr(radarr=radarr, sonarr=sonarr)

    movie = {"id": "m1", "tmdb_id": 100, "media_type": "movie"}
    tv = {"id": "t1", "tmdb_id": 200, "media_type": "tv"}
    all_recs = attach_download_states([_batch(movie, tv)], arr)

    # Radarr still resolves despite Sonarr being down.
    assert movie["download_state"] == "in_library"
    # Sonarr degraded — no state on the TV item.
    assert "download_state" not in tv
    assert set(all_recs) == {"m1", "t1"}


def test_attach_keeps_downloaded_at_when_arr_service_is_down():
    """A down service is treated as unconfigured: a freshly-optimistic
    ``downloaded_at`` must survive (we cannot confirm absence)."""
    from mediaman.services.infra import SafeHTTPError

    radarr = MagicMock()
    radarr.get_movies.side_effect = SafeHTTPError(
        503, "Radarr unreachable", "http://radarr/api/v3/movie"
    )
    arr = _FakeArr(radarr=radarr)

    movie = {
        "id": "m1",
        "tmdb_id": 100,
        "media_type": "movie",
        "downloaded_at": "2026-01-01T00:00:00+00:00",
    }
    attach_download_states([_batch(movie)], arr)

    assert movie["downloaded_at"] == "2026-01-01T00:00:00+00:00"


def test_attach_does_not_swallow_programming_errors():
    """A programming error (TypeError) from the client is a bug, not an
    expected upstream failure — it must still propagate, never be swallowed."""
    import pytest

    radarr = MagicMock()
    radarr.get_movies.side_effect = TypeError("bug in cache build")
    arr = _FakeArr(radarr=radarr)

    movie = {"id": "m1", "tmdb_id": 100, "media_type": "movie"}
    with pytest.raises(TypeError, match="bug in cache build"):
        attach_download_states([_batch(movie)], arr)


@pytest.mark.parametrize(
    ("wanted", "state"), [(0, "in_library"), (1, "partial")], ids=["skipped", "one episode wanted"]
)
def test_tv_unmonitored_aired_season_counts_only_by_an_episode_wanted_by_itself(wanted, state):
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
                    "episodeCount": wanted,
                    "totalEpisodeCount": 8,
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
    assert compute_download_state("tv", 800, caches) == state


def test_radarr_cache_queue_progress_from_size_and_sizeleft():
    radarr = MagicMock()
    radarr.get_movies.return_value = [{"tmdbId": 2}, {"tmdbId": 3}]
    radarr.get_queue.return_value = [
        {"movie": {"tmdbId": 2}, "size": 1000, "sizeleft": 580},
        {"movie": {"tmdbId": 3}, "size": 100, "sizeleft": 150},  # over-reported: clamps to 0
    ]
    cache = build_radarr_cache(radarr)
    assert cache["radarr_queue_progress"] == {2: 42, 3: 0}


def test_a_movie_whose_only_download_brings_nothing_is_not_downloading():
    radarr = MagicMock()
    radarr.get_movies.return_value = [{"tmdbId": n, "monitored": True} for n in (1, 2, 3, 4)]
    radarr.get_queue.return_value = [
        {"movie": {"tmdbId": 1}, "size": 100, "sizeleft": 0, "status": "failed"},
        {
            "movie": {"tmdbId": 2},
            "size": 100,
            "sizeleft": 60,
            "status": "downloadClientUnavailable",
        },
        {"movie": {"tmdbId": 3}, "size": 100, "sizeleft": 100, "status": "warning"},
        {"movie": {"tmdbId": 4}, "size": 100, "sizeleft": 60, "status": "warning"},
    ]
    caches = {**build_radarr_cache(radarr), **build_sonarr_cache(None)}
    states = [compute_download_state("movie", n, caches) for n in (1, 2, 3, 4)]
    assert states == ["queued", "queued", "queued", "downloading"]
    assert caches["radarr_queue_progress"] == {4: 40}


def test_a_finished_download_that_reports_no_size_is_downloading():
    radarr = MagicMock()
    radarr.get_movies.return_value = [{"tmdbId": 1, "monitored": True}]
    radarr.get_queue.return_value = [
        {"movie": {"tmdbId": 1}, "size": 0, "sizeleft": 0, "status": "completed"}
    ]
    caches = {**build_radarr_cache(radarr), **build_sonarr_cache(None)}
    assert compute_download_state("movie", 1, caches) == "downloading"


def test_sonarr_cache_queue_progress_sums_episodes_per_series():
    sonarr = MagicMock()
    sonarr.get_series.return_value = [{"tmdbId": 9}]
    sonarr.get_queue.return_value = [
        {"series": {"tmdbId": 9}, "size": 100, "sizeleft": 100},
        {"series": {"tmdbId": 9}, "size": 100, "sizeleft": 0},
        {"series": None, "size": 5, "sizeleft": 0},
    ]
    cache = build_sonarr_cache(sonarr)
    assert cache["sonarr_queue_progress"] == {9: 50}
    assert cache["sonarr_queue_tmdb_ids"] == {9}


def test_annotate_download_states_sets_pct_on_queued_and_downloading(monkeypatch):
    from mediaman.services.arr import build, state

    monkeypatch.setattr(build, "build_radarr_from_db", lambda conn, key: None)
    monkeypatch.setattr(build, "build_sonarr_from_db", lambda conn, key: None)
    monkeypatch.setattr(
        state,
        "build_radarr_cache",
        lambda client: {
            "radarr_movies": {1: {"tmdbId": 1, "monitored": True}, 2: {"tmdbId": 2}},
            "radarr_queue_tmdb_ids": {1},
            "radarr_queue_progress": {1: 42},
        },
    )
    monkeypatch.setattr(
        state,
        "build_sonarr_cache",
        lambda client: {
            "sonarr_series": {},
            "sonarr_queue_tmdb_ids": set(),
            "sonarr_queue_progress": {},
        },
    )
    results = [
        {"tmdb_id": 1, "media_type": "movie"},  # downloading
        {"tmdb_id": 2, "media_type": "movie"},  # queued, not in the queue yet
        {"tmdb_id": 3, "media_type": "movie"},  # untracked
    ]
    state.annotate_download_states(results, None, "k")
    assert results[0]["download_state"] == "downloading" and results[0]["download_pct"] == 42
    assert results[1]["download_state"] == "queued" and results[1]["download_pct"] == 0
    assert results[2]["download_state"] is None and "download_pct" not in results[2]


def _tv_caches(series, queued=False):
    return {
        "radarr_movies": {},
        "radarr_queue_tmdb_ids": set(),
        "sonarr_series": {500: series},
        "sonarr_queue_tmdb_ids": {500} if queued else set(),
    }


def _season(number, monitored=True, total=8, files=0, next_airing=None):
    stats = {"episodeCount": total, "episodeFileCount": files}
    if next_airing:
        stats["nextAiring"] = next_airing
    return {"seasonNumber": number, "monitored": monitored, "statistics": stats}


class TestSonarrQueuedOnlyWhenItWillFetch:
    def test_an_unmonitored_series_is_not_queued(self):
        series = {"tmdbId": 500, "monitored": False, "seasons": [_season(1)]}

        assert compute_download_state("tv", 500, _tv_caches(series)) is None

    def test_a_series_whose_every_season_is_unmonitored_is_not_queued(self):
        series = {
            "tmdbId": 500,
            "monitored": True,
            # Sonarr counts monitored episodes only, so an unmonitored season counts none.
            "seasons": [
                _season(1, monitored=False, total=0),
                _season(2, monitored=False, total=0),
            ],
        }

        assert compute_download_state("tv", 500, _tv_caches(series)) is None

    def test_a_monitored_season_that_lacks_files_keeps_it_queued(self):
        series = {
            "tmdbId": 500,
            "monitored": True,
            "seasons": [_season(1, monitored=False), _season(2, total=8, files=3)],
        }

        assert compute_download_state("tv", 500, _tv_caches(series)) == "queued"

    def test_monitored_seasons_that_are_complete_leave_nothing_to_fetch(self):
        series = {"tmdbId": 500, "monitored": True, "seasons": [_season(1, total=8, files=8)]}

        assert compute_download_state("tv", 500, _tv_caches(series)) is None

    def test_an_unmonitored_series_in_the_queue_is_still_downloading(self):
        series = {"tmdbId": 500, "monitored": False, "seasons": [_season(1)]}

        assert compute_download_state("tv", 500, _tv_caches(series, queued=True)) == "downloading"

    def test_an_entry_with_no_seasons_or_monitored_flag_stays_queued(self):
        assert compute_download_state("tv", 500, _tv_caches({"tmdbId": 500})) == "queued"

    def test_a_monitored_season_with_no_counts_stays_queued(self):
        series = {"tmdbId": 500, "seasons": [{"seasonNumber": 1, "monitored": True}]}

        assert compute_download_state("tv", 500, _tv_caches(series)) == "queued"

    def test_an_unmonitored_season_with_single_monitored_episodes_stays_queued(self):
        series = {"tmdbId": 500, "monitored": True, "seasons": [_season(1, False, total=1)]}

        assert compute_download_state("tv", 500, _tv_caches(series)) == "queued"

    def test_an_unmonitored_season_with_a_monitored_episode_still_to_air_stays_queued(self):
        series = {
            "tmdbId": 500,
            "monitored": True,
            "seasons": [_season(1, False, total=0, next_airing="2099-01-01T00:00:00Z")],
        }

        assert compute_download_state("tv", 500, _tv_caches(series)) == "queued"

    def test_one_early_file_with_the_rest_unaired_stays_queued(self):
        series = {
            "tmdbId": 500,
            "monitored": True,
            "seasons": [_season(1, True, total=1, files=1, next_airing="2099-01-01T00:00:00Z")],
        }

        assert compute_download_state("tv", 500, _tv_caches(series)) == "queued"

    def test_complete_specials_alone_are_not_queued(self):
        series = {"tmdbId": 500, "monitored": True, "seasons": [_season(0, True, total=3, files=3)]}

        assert compute_download_state("tv", 500, _tv_caches(series)) is None

    def test_missing_specials_stay_queued(self):
        series = {"tmdbId": 500, "monitored": True, "seasons": [_season(0, True, total=3, files=1)]}

        assert compute_download_state("tv", 500, _tv_caches(series)) == "queued"

    def test_an_unmonitored_series_with_an_episode_to_air_is_still_none(self):
        series = {
            "tmdbId": 500,
            "monitored": False,
            "seasons": [_season(1, True, total=1, next_airing="2099-01-01T00:00:00Z")],
        }

        assert compute_download_state("tv", 500, _tv_caches(series)) is None


class TestAddOrRemonitorMovieWhenTheSearchFails:
    def _radarr(self):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 7, "monitored": False}
        radarr.remonitor_movie.side_effect = SafeHTTPError(400, "search refused", "radarr/command")
        return radarr

    def test_the_movie_is_put_back_to_unmonitored_and_the_error_still_raised(self):
        radarr = self._radarr()

        with pytest.raises(ExistingMovieError):
            add_or_remonitor_movie(radarr, 603, "The Matrix")

        radarr.unmonitor_movie.assert_called_once_with(7, max_retries=1)

    def test_a_movie_whose_search_may_be_running_stays_monitored_and_counts_as_re_monitored(self):
        radarr = self._radarr()
        radarr.remonitor_movie.side_effect = SearchUnanswered("no answer")

        assert add_or_remonitor_movie(radarr, 603, "The Matrix") == "remonitored"

        radarr.unmonitor_movie.assert_not_called()

    def test_a_failing_put_back_is_logged_and_does_not_hide_the_error(self, caplog):
        radarr = self._radarr()
        radarr.unmonitor_movie.side_effect = SafeHTTPError(500, "down", "radarr/movie")

        with pytest.raises(ExistingMovieError):
            add_or_remonitor_movie(radarr, 603, "The Matrix")

        assert "back to unmonitored" in caplog.text

    def test_a_movie_that_re_monitored_cleanly_is_left_monitored(self):
        radarr = self._radarr()
        radarr.remonitor_movie.side_effect = None

        assert add_or_remonitor_movie(radarr, 603, "The Matrix") == "remonitored"
        radarr.unmonitor_movie.assert_not_called()
