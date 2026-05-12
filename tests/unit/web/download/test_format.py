"""Tests for download format helpers.

Covers pure logic in :mod:`mediaman.services.downloads.download_format`:
- map_state
- build_item
- select_hero
- looks_like_series_nzb
- classify_movie_upcoming
- classify_series_upcoming

Also covers completion detection (:mod:`mediaman.services.arr.completion`).
"""

from __future__ import annotations

import pytest

from mediaman.services.arr.completion import detect_completed
from mediaman.services.downloads.download_format import (
    build_item,
    classify_movie_upcoming,
    classify_series_upcoming,
    looks_like_series_nzb,
    map_state,
    select_hero,
)
from mediaman.services.downloads.download_queue import _reset_previous_queue


class TestStateMapping:
    def test_searching_state(self):
        """Item in Arr queue with no NZBGet match → searching."""
        assert map_state(nzbget_status=None, has_nzbget_match=False) == "searching"

    def test_downloading_state(self):
        """NZBGet status contains DOWNLOADING → downloading."""
        assert map_state(nzbget_status="DOWNLOADING", has_nzbget_match=True) == "downloading"

    def test_almost_ready_unpacking(self):
        """NZBGet status contains UNPACKING → almost_ready."""
        assert map_state(nzbget_status="UNPACKING", has_nzbget_match=True) == "almost_ready"

    def test_almost_ready_postprocessing(self):
        """NZBGet status contains PP_ → almost_ready."""
        assert map_state(nzbget_status="PP_QUEUED", has_nzbget_match=True) == "almost_ready"

    def test_queued_state(self):
        """NZBGet status is QUEUED → searching (not yet actively downloading)."""
        assert map_state(nzbget_status="QUEUED", has_nzbget_match=True) == "searching"

    def test_paused_state(self):
        """NZBGet status is PAUSED → downloading (still has progress)."""
        assert map_state(nzbget_status="PAUSED", has_nzbget_match=True) == "downloading"


class TestBuildItem:
    def test_movie_item_shape(self):
        """A movie item has the expected fields."""
        item = build_item(
            dl_id="radarr:Dune",
            title="Dune: Part Two",
            media_type="movie",
            poster_url="https://example.com/poster.jpg",
            state="downloading",
            progress=67,
            eta="~12 min remaining",
            size_done="4.2 GB",
            size_total="6.3 GB",
        )
        assert item["id"] == "radarr:Dune"
        assert item["title"] == "Dune: Part Two"
        assert item["media_type"] == "movie"
        assert item["state"] == "downloading"
        assert item["progress"] == 67
        assert item["episodes"] is None

    def test_series_item_has_episodes(self):
        """A series item includes the episodes list."""
        episodes = [
            {"label": "S03E01", "title": "Pilot", "state": "ready", "progress": 100},
            {"label": "S03E02", "title": "Two", "state": "downloading", "progress": 45},
        ]
        item = build_item(
            dl_id="sonarr:Severance",
            title="Severance",
            media_type="series",
            poster_url="",
            state="downloading",
            progress=72,
            eta="~20 min remaining",
            size_done="3.4 GB",
            size_total="4.7 GB",
            episodes=episodes,
        )
        assert item["media_type"] == "series"
        assert len(item["episodes"]) == 2
        assert item["episodes"][0]["state"] == "ready"

    def test_upcoming_item_has_release_label(self):
        item = build_item(
            dl_id="radarr:FutureFilm",
            title="Future Film",
            media_type="movie",
            poster_url="",
            state="upcoming",
            progress=0,
            eta="",
            size_done="",
            size_total="",
            release_label="Releases 14 Jun 2099",
        )
        assert item["state"] == "upcoming"
        assert item["release_label"] == "Releases 14 Jun 2099"

    def test_default_release_label_is_empty(self):
        item = build_item(
            dl_id="radarr:Dune",
            title="Dune",
            media_type="movie",
            poster_url="",
            state="downloading",
            progress=50,
            eta="",
            size_done="",
            size_total="",
        )
        assert item["release_label"] == ""


class TestHeroSelection:
    def test_single_item_is_hero(self):
        """A single item in the queue becomes the hero."""
        items = [
            build_item(
                dl_id="r:A",
                title="A",
                media_type="movie",
                poster_url="",
                state="downloading",
                progress=50,
                eta="",
                size_done="",
                size_total="",
            )
        ]
        hero, rest = select_hero(items)
        assert hero["id"] == "r:A"
        assert rest == []

    def test_highest_progress_downloading_is_hero(self):
        """The actively downloading item with the highest progress wins."""
        items = [
            build_item(
                dl_id="r:A",
                title="A",
                media_type="movie",
                poster_url="",
                state="searching",
                progress=0,
                eta="",
                size_done="",
                size_total="",
            ),
            build_item(
                dl_id="r:B",
                title="B",
                media_type="movie",
                poster_url="",
                state="downloading",
                progress=30,
                eta="",
                size_done="",
                size_total="",
            ),
            build_item(
                dl_id="r:C",
                title="C",
                media_type="movie",
                poster_url="",
                state="downloading",
                progress=80,
                eta="",
                size_done="",
                size_total="",
            ),
        ]
        hero, rest = select_hero(items)
        assert hero["id"] == "r:C"
        assert len(rest) == 2

    def test_no_downloading_picks_first(self):
        """When all items are searching, the first item is the hero."""
        items = [
            build_item(
                dl_id="r:A",
                title="A",
                media_type="movie",
                poster_url="",
                state="searching",
                progress=0,
                eta="",
                size_done="",
                size_total="",
            ),
            build_item(
                dl_id="r:B",
                title="B",
                media_type="movie",
                poster_url="",
                state="searching",
                progress=0,
                eta="",
                size_done="",
                size_total="",
            ),
        ]
        hero, rest = select_hero(items)
        assert hero["id"] == "r:A"
        assert len(rest) == 1

    def test_empty_queue_returns_none(self):
        """Empty queue returns None hero."""
        hero, rest = select_hero([])
        assert hero is None
        assert rest == []


class TestCompletionDetection:
    @pytest.fixture(autouse=True)
    def _reset_queue(self):
        """Reset state between tests."""
        _reset_previous_queue()

    def test_item_disappearing_is_completed(self):
        """An item present previously but absent now is detected as completed."""
        previous = {
            "radarr:Dune": {"id": "radarr:Dune", "title": "Dune", "kind": "movie", "poster_url": ""}
        }
        current = {}
        completed = detect_completed(previous, current)
        assert len(completed) == 1
        assert completed[0]["dl_id"] == "radarr:Dune"

    def test_no_change_means_no_completions(self):
        """Same items in both snapshots → nothing completed."""
        snapshot = {
            "radarr:Dune": {"id": "radarr:Dune", "title": "Dune", "kind": "movie", "poster_url": ""}
        }
        completed = detect_completed(snapshot, snapshot)
        assert completed == []

    def test_new_item_is_not_completed(self):
        """An item appearing for the first time is not a completion."""
        previous = {}
        current = {
            "radarr:Dune": {"id": "radarr:Dune", "title": "Dune", "kind": "movie", "poster_url": ""}
        }
        completed = detect_completed(previous, current)
        assert completed == []

    def test_reset_clears_previous(self):
        """_reset_previous_queue clears the in-memory snapshot."""
        _reset_previous_queue()  # Should not raise


class TestLooksLikeSeriesNzb:
    def test_sxxexx_marker_matches(self):
        assert looks_like_series_nzb("Love.Island.S06E13.1080p.WEB.mkv")

    def test_season_only_marker_matches(self):
        assert looks_like_series_nzb("The.Great.S02.Complete.1080p")

    def test_movie_style_name_does_not_match(self):
        assert not looks_like_series_nzb("The.Great.Gatsby.2013.1080p.BluRay.x264.mkv")

    def test_empty_string_does_not_match(self):
        assert not looks_like_series_nzb("")


class TestClassifyMovieUpcoming:
    def test_not_available_movie_is_upcoming(self):
        movie = {
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "digitalRelease": "2099-06-14T00:00:00Z",
        }
        is_upcoming, label = classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert label.startswith("Releases ")
        assert "2099" in label

    def test_available_movie_is_not_upcoming(self):
        movie = {"monitored": True, "hasFile": False, "isAvailable": True}
        is_upcoming, label = classify_movie_upcoming(movie)
        assert is_upcoming is False
        assert label == ""

    def test_unmonitored_movie_is_not_upcoming(self):
        movie = {"monitored": False, "hasFile": False, "isAvailable": False}
        is_upcoming, _label = classify_movie_upcoming(movie)
        assert is_upcoming is False

    def test_already_has_file_is_not_upcoming(self):
        movie = {"monitored": True, "hasFile": True, "isAvailable": False}
        is_upcoming, _label = classify_movie_upcoming(movie)
        assert is_upcoming is False

    def test_upcoming_with_no_release_dates_has_fallback_label(self):
        movie = {"monitored": True, "hasFile": False, "isAvailable": False}
        is_upcoming, label = classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert label == "Not yet released"

    def test_label_picks_earliest_future_date(self):
        movie = {
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "digitalRelease": "2099-06-14T00:00:00Z",
            "physicalRelease": "2099-09-01T00:00:00Z",
            "inCinemas": "2099-03-15T00:00:00Z",
        }
        is_upcoming, label = classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert "2099" in label
        assert "Mar" in label

    def test_label_ignores_past_dates(self):
        movie = {
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "inCinemas": "1999-01-01T00:00:00Z",
            "digitalRelease": "2099-12-01T00:00:00Z",
        }
        is_upcoming, label = classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert "2099" in label
        assert "Dec" in label

    def test_label_all_past_dates_falls_back(self):
        movie = {
            "monitored": True,
            "hasFile": False,
            "isAvailable": False,
            "digitalRelease": "1999-01-01T00:00:00Z",
            "physicalRelease": "2000-01-01T00:00:00Z",
            "inCinemas": "2001-01-01T00:00:00Z",
        }
        is_upcoming, label = classify_movie_upcoming(movie)
        assert is_upcoming is True
        assert label == "Not yet released"


class TestClassifySeriesUpcoming:
    def test_upcoming_status_is_upcoming(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 0},
        }
        is_upcoming, _label = classify_series_upcoming(series, episodes=[])
        assert is_upcoming is True

    def test_continuing_with_aired_episodes_is_not_upcoming(self):
        series = {
            "monitored": True,
            "status": "continuing",
            "statistics": {"episodeFileCount": 0},
        }
        episodes = [{"airDateUtc": "2020-01-01T00:00:00Z"}]
        is_upcoming, _label = classify_series_upcoming(series, episodes=episodes)
        assert is_upcoming is False

    def test_unmonitored_is_not_upcoming(self):
        series = {"monitored": False, "status": "upcoming"}
        is_upcoming, _label = classify_series_upcoming(series, episodes=[])
        assert is_upcoming is False

    def test_has_episode_files_is_not_upcoming(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 3},
        }
        is_upcoming, _label = classify_series_upcoming(series, episodes=[])
        assert is_upcoming is False

    def test_all_future_episodes_with_continuing_status_is_upcoming(self):
        series = {
            "monitored": True,
            "status": "continuing",
            "statistics": {"episodeFileCount": 0},
        }
        episodes = [{"airDateUtc": "2099-12-01T00:00:00Z"}]
        is_upcoming, label = classify_series_upcoming(series, episodes=episodes)
        assert is_upcoming is True
        assert "2099" in label
        assert label.startswith("Premieres ")

    def test_upcoming_label_with_no_air_dates_has_fallback(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 0},
        }
        is_upcoming, label = classify_series_upcoming(series, episodes=[])
        assert is_upcoming is True
        assert label == "Not yet aired"

    def test_label_picks_earliest_future_airdate(self):
        series = {
            "monitored": True,
            "status": "upcoming",
            "statistics": {"episodeFileCount": 0},
        }
        episodes = [
            {"airDateUtc": "2099-12-01T00:00:00Z"},
            {"airDateUtc": "2099-03-15T00:00:00Z"},
            {"airDateUtc": "2099-06-14T00:00:00Z"},
        ]
        is_upcoming, label = classify_series_upcoming(series, episodes=episodes)
        assert is_upcoming is True
        assert "Mar" in label
        assert "2099" in label

    def test_ended_series_with_empty_episodes_is_not_upcoming(self):
        """An ended/continuing series with no episodes fetched is NOT classified as upcoming.

        Protects against misclassifying series whose episode metadata hasn't loaded yet
        or whose get_episodes() call failed.
        """
        series = {
            "monitored": True,
            "status": "ended",
            "statistics": {"episodeFileCount": 0},
        }
        is_upcoming, label = classify_series_upcoming(series, episodes=[])
        assert is_upcoming is False
        assert label == ""
