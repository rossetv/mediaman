"""Tests for mediaman.services.downloads.download_queue._deep_links."""

from __future__ import annotations

from mediaman.services.downloads.download_queue._deep_links import (
    build_arr_link,
    build_search_hint,
)

# ---------------------------------------------------------------------------
# build_search_hint
# ---------------------------------------------------------------------------


class TestBuildSearchHint:
    def test_searched_once(self):
        now = 1_000_000
        ts = now - 600  # 10 min ago
        result = build_search_hint(search_count=1, last_search_ts=ts, added_at=0, now=now)
        assert "Searched once" in result
        assert "10m ago" in result

    def test_searched_multiple_times(self):
        now = 1_000_000
        ts = now - 3600  # 1 h ago
        result = build_search_hint(search_count=3, last_search_ts=ts, added_at=0, now=now)
        assert "Searched 3×" in result

    def test_falls_back_to_added_at_when_no_search(self):
        now = 1_000_000
        added = now - 600  # 10 min ago
        result = build_search_hint(search_count=0, last_search_ts=0, added_at=added, now=now)
        assert "Added" in result
        assert "first search" in result

    def test_returns_empty_when_nothing_available(self):
        result = build_search_hint(search_count=0, last_search_ts=0, added_at=0, now=1_000_000)
        assert result == ""

    def test_zero_search_ts_falls_back_to_added(self):
        now = 1_000_000
        added = now - 7200  # 2 h ago
        result = build_search_hint(search_count=1, last_search_ts=0, added_at=added, now=now)
        # last_search_ts=0 is falsy → falls back to added_at
        assert "Added" in result


# ---------------------------------------------------------------------------
# build_arr_link
# ---------------------------------------------------------------------------


class TestBuildArrLink:
    def test_movie_link_built_correctly(self):
        arr = {"kind": "movie", "title_slug": "dune-2021"}
        base_urls = {"radarr": "http://radarr.local:7878", "sonarr": ""}
        link = build_arr_link(arr, base_urls)
        assert link == "http://radarr.local:7878/movie/dune-2021"

    def test_series_link_built_correctly(self):
        arr = {"kind": "series", "title_slug": "breaking-bad"}
        base_urls = {"radarr": "", "sonarr": "http://sonarr.local:8989"}
        link = build_arr_link(arr, base_urls)
        assert link == "http://sonarr.local:8989/series/breaking-bad"

    def test_no_slug_returns_empty(self):
        arr = {"kind": "movie", "title_slug": ""}
        base_urls = {"radarr": "http://radarr.local"}
        assert build_arr_link(arr, base_urls) == ""

    def test_no_base_url_returns_empty(self):
        arr = {"kind": "movie", "title_slug": "dune-2021"}
        base_urls = {"radarr": "", "sonarr": ""}
        assert build_arr_link(arr, base_urls) == ""

    def test_trailing_slash_stripped_from_base_url(self):
        arr = {"kind": "movie", "title_slug": "dune-2021"}
        base_urls = {"radarr": "http://radarr.local/", "sonarr": ""}
        link = build_arr_link(arr, base_urls)
        assert not link.startswith("http://radarr.local//")

    def test_missing_title_slug_key_returns_empty(self):
        """An arr dict without a title_slug key must not raise."""
        arr = {"kind": "movie"}
        base_urls = {"radarr": "http://radarr.local"}
        assert build_arr_link(arr, base_urls) == ""
