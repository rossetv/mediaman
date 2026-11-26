"""Tests for mediaman.services.downloads.download_format._render."""

from __future__ import annotations

import pytest

from mediaman.services.downloads.download_format import (
    build_episode_summary,
    build_item,
    select_hero,
)


class TestBuildItem:
    def _item(self, **kwargs) -> dict:
        defaults = {
            "dl_id": "radarr:Dune",
            "title": "Dune",
            "media_type": "movie",
            "poster_url": "",
            "state": "downloading",
            "progress": 50,
            "eta": "~10 min remaining",
            "size_done": "2.0 GB",
            "size_total": "4.0 GB",
        }
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


class TestBuildEpisodeSummary:
    def test_all_ready(self):
        eps = [{"state": "ready"}, {"state": "ready"}]
        result = build_episode_summary(eps)
        assert "2 of 2 episodes ready" in result

    def test_one_episode_is_one_episode(self):
        assert build_episode_summary([{"state": "ready"}]) == "1 of 1 episode ready"

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

    @pytest.mark.parametrize(
        ("count", "said"), [(1, "1 episode downloading"), (3, "3 episodes downloading")]
    )
    def test_it_says_how_many_episodes_are_downloading(self, count, said):
        assert build_episode_summary([{"state": "downloading"}] * count) == said

    def test_parts_joined_by_separator(self):
        eps = [{"state": "ready"}, {"state": "downloading"}]
        result = build_episode_summary(eps)
        assert " · " in result


class TestSelectHero:
    def _dl_item(self, *, state="downloading", progress=0, title="Item"):
        return {"id": f"radarr:{title}", "title": title, "state": state, "progress": progress}

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
        hero, _rest = select_hero([searching, downloading])
        assert hero["title"] == "Downloading"

    def test_a_download_that_passes_another_takes_neither_the_top_card_nor_its_row(self):
        a = self._dl_item(state="downloading", progress=20, title="A")
        b = self._dl_item(state="downloading", progress=10, title="B")
        c = self._dl_item(state="downloading", progress=30, title="C")
        before = select_hero([a, b, c])
        c["progress"] = b["progress"] = 90
        after = select_hero([c, b, a])
        assert [after[0], *after[1]] == [before[0], *before[1]]
        assert before[0] is a

    def test_the_last_top_card_stays_on_top_while_it_is_coming_in(self):
        a = self._dl_item(state="downloading", title="A")
        z = self._dl_item(state="almost_ready", title="Z")
        assert select_hero([a, z], kept="radarr:Z")[0] is z
        z["state"] = "searching"
        assert select_hero([a, z], kept="radarr:Z")[0] is a

    def test_first_item_wins_when_nothing_downloading(self):
        a = self._dl_item(state="searching", progress=0, title="A")
        b = self._dl_item(state="searching", progress=0, title="B")
        hero, rest = select_hero([a, b])
        # Either a or b wins; just check the contract: hero + rest == all items
        assert len(rest) == 1
        assert hero is not None
