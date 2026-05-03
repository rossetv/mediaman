"""Tests for mediaman.services.downloads.download_queue._items."""

from __future__ import annotations

from unittest.mock import patch

from mediaman.services.arr.fetcher._radarr import _make_radarr_card
from mediaman.services.arr.fetcher._sonarr import _make_sonarr_card
from mediaman.services.downloads.download_queue._items import (
    build_episode_dicts,
    build_matched_item,
    build_unmatched_arr_item,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ep_entry(
    label="S01E01",
    title="Pilot",
    progress=100,
    sizeleft=0,
    size=500_000_000,
    status="completed",
    is_pack=False,
) -> dict:
    return {
        "label": label,
        "title": title,
        "progress": progress,
        "sizeleft": sizeleft,
        "size": size,
        "status": status,
        "is_pack_episode": is_pack,
    }


def _nzb(title="Dune", progress=50, done_mb=2048, file_mb=4096, dl_id="nzb-001") -> dict:
    return {
        "title": title,
        "progress": progress,
        "done_mb": done_mb,
        "file_mb": file_mb,
        "dl_id": dl_id,
    }


def _fake_search_hint(*args, **kwargs) -> str:
    return ""


def _fake_arr_link(arr, base_urls) -> str:
    return ""


# ---------------------------------------------------------------------------
# build_episode_dicts
# ---------------------------------------------------------------------------


class TestBuildEpisodeDicts:
    def test_maps_label_and_title(self):
        eps = [_ep_entry(label="S01E01", title="Pilot")]
        result = build_episode_dicts(eps)
        assert result[0]["label"] == "S01E01"
        assert result[0]["title"] == "Pilot"

    def test_state_derived_from_map_episode_state(self):
        """Fully downloaded episode (progress=100) → state=='ready'."""
        eps = [_ep_entry(progress=100, sizeleft=0, size=500_000_000)]
        result = build_episode_dicts(eps)
        assert result[0]["state"] == "ready"

    def test_pack_episode_flag_preserved(self):
        eps = [_ep_entry(is_pack=True)]
        result = build_episode_dicts(eps)
        assert result[0]["is_pack_episode"] is True

    def test_empty_list_returns_empty(self):
        assert build_episode_dicts([]) == []


# ---------------------------------------------------------------------------
# build_matched_item — movie
# ---------------------------------------------------------------------------


class TestBuildMatchedItemMovie:
    def test_produces_movie_media_type(self):
        arr = _make_radarr_card("Dune", year=2021, progress=50)
        item = build_matched_item(
            arr, _nzb(), state="downloading", eta="~10 min", download_rate=1_000_000
        )
        assert item["media_type"] == "movie"

    def test_title_from_arr_preferred(self):
        arr = _make_radarr_card("Dune", year=2021)
        nzb = _nzb(title="Dune.2021.1080p.BluRay")
        item = build_matched_item(arr, nzb, state="downloading", eta="", download_rate=0)
        assert item["title"] == "Dune"

    def test_progress_taken_from_nzb(self):
        arr = _make_radarr_card("Dune", year=2021)
        item = build_matched_item(
            arr, _nzb(progress=75), state="downloading", eta="", download_rate=0
        )
        assert item["progress"] == 75


# ---------------------------------------------------------------------------
# build_matched_item — series
# ---------------------------------------------------------------------------


class TestBuildMatchedItemSeries:
    def test_produces_series_media_type(self):
        arr = _make_sonarr_card("Breaking Bad", episodes=[_ep_entry()])
        item = build_matched_item(arr, _nzb(), state="downloading", eta="", download_rate=0)
        assert item["media_type"] == "series"

    def test_episodes_populated(self):
        arr = _make_sonarr_card("Breaking Bad", episodes=[_ep_entry(), _ep_entry(label="S01E02")])
        item = build_matched_item(arr, _nzb(), state="downloading", eta="", download_rate=0)
        assert len(item["episodes"]) == 2


# ---------------------------------------------------------------------------
# build_unmatched_arr_item
# ---------------------------------------------------------------------------


class TestBuildUnmatchedArrItem:
    @patch("mediaman.services.arr.search_trigger.get_search_info", return_value=(0, 0.0))
    def test_unmatched_movie_in_searching_state(self, _mock_search):
        arr = _make_radarr_card("Dune", progress=0)
        item = build_unmatched_arr_item(arr, {}, _fake_search_hint, _fake_arr_link)
        assert item["media_type"] == "movie"
        assert item["state"] == "searching"

    @patch("mediaman.services.arr.search_trigger.get_search_info", return_value=(0, 0.0))
    def test_unmatched_movie_at_100_is_almost_ready(self, _mock_search):
        arr = _make_radarr_card("Dune", progress=100)
        item = build_unmatched_arr_item(arr, {}, _fake_search_hint, _fake_arr_link)
        assert item["state"] == "almost_ready"

    @patch("mediaman.services.arr.search_trigger.get_search_info", return_value=(0, 0.0))
    def test_unmatched_series_in_searching_state(self, _mock_search):
        arr = _make_sonarr_card("Breaking Bad", episodes=[])
        item = build_unmatched_arr_item(arr, {}, _fake_search_hint, _fake_arr_link)
        assert item["media_type"] == "series"
        assert item["state"] == "searching"

    @patch("mediaman.services.arr.search_trigger.get_search_info", return_value=(0, 0.0))
    def test_all_ready_episodes_produce_almost_ready(self, _mock_search):
        """When all episodes are downloaded, the series card state is 'almost_ready'."""
        eps = [
            _ep_entry(progress=100, sizeleft=0, size=500_000_000),
            _ep_entry(label="S01E02", progress=100, sizeleft=0, size=500_000_000),
        ]
        arr = _make_sonarr_card("Silo", episodes=eps)
        item = build_unmatched_arr_item(arr, {}, _fake_search_hint, _fake_arr_link)
        assert item["state"] == "almost_ready"

    @patch("mediaman.services.arr.search_trigger.get_search_info", return_value=(3, 1_000_000.0))
    def test_search_count_propagated(self, _mock_search):
        arr = _make_radarr_card("Dune", progress=0)
        item = build_unmatched_arr_item(arr, {}, _fake_search_hint, _fake_arr_link)
        assert item["search_count"] == 3


# ---------------------------------------------------------------------------
# abandon_visible — time-based threshold
# ---------------------------------------------------------------------------

_TEN_HOURS = 10 * 3600


class TestAbandonVisibleThreshold:
    """abandon_visible flips on once an item has been searching for ≥10 h."""

    def _make_item(self, added_at: float, progress: int = 0) -> dict:
        arr = _make_radarr_card("Dune", progress=progress)
        arr["added_at"] = added_at
        with patch(
            "mediaman.services.arr.search_trigger.get_search_info",
            return_value=(5, 1.0),
        ):
            return build_unmatched_arr_item(arr, {}, _fake_search_hint, _fake_arr_link)

    def test_hidden_when_added_under_ten_hours_ago(self):
        now = __import__("time").time()
        item = self._make_item(added_at=now - _TEN_HOURS + 3600)  # 9 h ago
        assert item["abandon_visible"] is False

    def test_hidden_just_before_ten_hours(self):
        now = __import__("time").time()
        item = self._make_item(added_at=now - _TEN_HOURS + 60)  # 1 min short
        assert item["abandon_visible"] is False

    def test_visible_at_ten_hours(self):
        now = __import__("time").time()
        item = self._make_item(added_at=now - _TEN_HOURS)
        assert item["abandon_visible"] is True

    def test_visible_well_past_ten_hours(self):
        now = __import__("time").time()
        item = self._make_item(added_at=now - 48 * 3600)  # 2 days
        assert item["abandon_visible"] is True

    def test_hidden_when_state_is_not_searching(self):
        """Even after 10 h, non-searching items must not show the button."""
        now = __import__("time").time()
        item = self._make_item(added_at=now - 48 * 3600, progress=100)
        assert item["abandon_visible"] is False

    def test_abandon_escalated_key_absent(self):
        """abandon_escalated must not appear in the item dict at all."""
        now = __import__("time").time()
        item = self._make_item(added_at=now - 48 * 3600)
        assert "abandon_escalated" not in item


# ---------------------------------------------------------------------------
# stuck_seasons derivation
# ---------------------------------------------------------------------------


def _ep_with_season(label: str, season_number: int) -> dict:
    """Episode entry in 'searching' state — size=0, no active status."""
    return {
        "label": label,
        "season_number": season_number,
        "title": "",
        "size": 0,
        "sizeleft": 0,
        "size_str": "",
        "status": "",
        "progress": 0,
        "download_id": "",
    }


class TestStuckSeasons:
    @patch("mediaman.services.arr.search_trigger.get_search_info", return_value=(0, 0.0))
    def test_movie_has_empty_stuck_seasons(self, _mock_search):
        arr = _make_radarr_card("Dune", progress=0)
        item = build_unmatched_arr_item(arr, {}, _fake_search_hint, _fake_arr_link)
        assert item["stuck_seasons"] == []

    @patch("mediaman.services.arr.search_trigger.get_search_info", return_value=(0, 0.0))
    def test_series_groups_episodes_by_season(self, _mock_search):
        arr = _make_sonarr_card(
            "Breaking Bad",
            episodes=[
                _ep_with_season("S21E01", 21),
                _ep_with_season("S21E02", 21),
                _ep_with_season("S22E01", 22),
            ],
        )
        item = build_unmatched_arr_item(arr, {}, _fake_search_hint, _fake_arr_link)
        assert item["stuck_seasons"] == [
            {"number": 21, "missing_episodes": 2},
            {"number": 22, "missing_episodes": 1},
        ]

    @patch("mediaman.services.arr.search_trigger.get_search_info", return_value=(0, 0.0))
    def test_series_stuck_seasons_empty_when_not_searching(self, _mock_search):
        """stuck_seasons should be empty when the series is not in searching state."""
        eps = [
            {
                **_ep_with_season("S01E01", 1),
                "progress": 100,
                "sizeleft": 0,
                "size": 500_000_000,
                "status": "completed",
            },
            {
                **_ep_with_season("S01E02", 1),
                "progress": 100,
                "sizeleft": 0,
                "size": 500_000_000,
                "status": "completed",
            },
        ]
        arr = _make_sonarr_card("Silo", episodes=eps)
        item = build_unmatched_arr_item(arr, {}, _fake_search_hint, _fake_arr_link)
        # state will be almost_ready — stuck_seasons must be empty
        assert item["state"] == "almost_ready"
        assert item["stuck_seasons"] == []
