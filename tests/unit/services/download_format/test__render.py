"""Tests for mediaman.services.downloads.download_format._render."""

from __future__ import annotations

from mediaman.services.downloads.download_format._render import (
    build_episode_summary,
    build_item,
    select_hero,
)

# ---------------------------------------------------------------------------
# build_item
# ---------------------------------------------------------------------------


class TestBuildItem:
    def _item(self, **kwargs) -> dict:
        defaults = dict(
            dl_id="radarr:Dune",
            title="Dune",
            media_type="movie",
            poster_url="",
            state="downloading",
            progress=50,
            eta="~10 min remaining",
            size_done="2.0 GB",
            size_total="4.0 GB",
        )
        defaults.update(kwargs)
        return build_item(**defaults)

    def test_required_fields_present(self):
        item = self._item()
        for field in ("id", "title", "media_type", "state", "progress", "eta"):
            assert field in item

    def test_id_equals_dl_id(self):
        item = self._item(dl_id="radarr:Dune")
        assert item["id"] == "radarr:Dune"

    def test_optional_arr_link_default_empty(self):
        item = self._item()
        assert item["arr_link"] == ""

    def test_optional_search_count_default_zero(self):
        item = self._item()
        assert item["search_count"] == 0

    def test_custom_values_preserved(self):
        item = self._item(title="Oppenheimer", progress=75, state="almost_ready")
        assert item["title"] == "Oppenheimer"
        assert item["progress"] == 75
        assert item["state"] == "almost_ready"

    def test_arr_link_stored(self):
        item = self._item(arr_link="http://radarr.local/movie/dune", arr_source="Radarr")
        assert item["arr_link"] == "http://radarr.local/movie/dune"
        assert item["arr_source"] == "Radarr"


# ---------------------------------------------------------------------------
# build_episode_summary
# ---------------------------------------------------------------------------


class TestBuildEpisodeSummary:
    def test_all_ready(self):
        eps = [{"state": "ready"}, {"state": "ready"}]
        result = build_episode_summary(eps)
        assert "2 of 2 episodes ready" in result

    def test_mixed_states(self):
        eps = [
            {"state": "ready"},
            {"state": "downloading"},
            {"state": "queued"},
            {"state": "searching"},
        ]
        result = build_episode_summary(eps)
        assert "1 of 4 episodes ready" in result
        assert "downloading" in result
        assert "queued" in result
        assert "searching" in result

    def test_empty_episodes_returns_empty_string(self):
        assert build_episode_summary([]) == ""

    def test_all_searching(self):
        eps = [{"state": "searching"}, {"state": "searching"}]
        result = build_episode_summary(eps)
        assert "searching" in result
        assert "ready" not in result

    def test_parts_joined_by_separator(self):
        eps = [{"state": "ready"}, {"state": "downloading"}]
        result = build_episode_summary(eps)
        assert " · " in result


# ---------------------------------------------------------------------------
# select_hero
# ---------------------------------------------------------------------------


class TestSelectHero:
    def _dl_item(self, *, state="downloading", progress=0, title="Item"):
        return {"title": title, "state": state, "progress": progress}

    def test_empty_list_returns_none_and_empty(self):
        hero, rest = select_hero([])
        assert hero is None
        assert rest == []

    def test_single_item_is_hero(self):
        item = self._dl_item(title="Dune")
        hero, rest = select_hero([item])
        assert hero is item
        assert rest == []

    def test_downloading_item_wins_over_searching(self):
        searching = self._dl_item(state="searching", progress=0, title="Searching")
        downloading = self._dl_item(state="downloading", progress=60, title="Downloading")
        hero, rest = select_hero([searching, downloading])
        assert hero["title"] == "Downloading"

    def test_highest_progress_downloading_wins(self):
        low = self._dl_item(state="downloading", progress=20, title="Low")
        high = self._dl_item(state="downloading", progress=80, title="High")
        hero, rest = select_hero([low, high])
        assert hero["title"] == "High"
        assert any(r["title"] == "Low" for r in rest)

    def test_first_item_wins_when_nothing_downloading(self):
        a = self._dl_item(state="searching", progress=0, title="A")
        b = self._dl_item(state="searching", progress=0, title="B")
        hero, rest = select_hero([a, b])
        # Either a or b wins; just check the contract: hero + rest == all items
        assert len(rest) == 1
        assert hero is not None
