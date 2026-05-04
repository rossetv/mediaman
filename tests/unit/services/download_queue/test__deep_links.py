"""Tests for mediaman.services.downloads.download_queue.classify."""

from __future__ import annotations

from mediaman.services.downloads.download_queue.classify import (
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
        # new copy: "next attempt in X" or "firing now"
        assert "next attempt" in result or "firing now" in result

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


# ---------------------------------------------------------------------------
# TestSearchHintNextAttempt
# ---------------------------------------------------------------------------


class TestSearchHintNextAttempt:
    """build_search_hint surfaces 'next attempt in X' derived from the backoff curve."""

    def test_first_search_pending_keeps_legacy_copy(self):
        out = build_search_hint(
            search_count=0, last_search_ts=0.0, added_at=1700000000.0, now=1700000900.0
        )
        assert "waiting for first search" in out

    def test_minutes_format_under_one_hour(self, monkeypatch):
        from mediaman.services.downloads.download_queue import classify

        monkeypatch.setattr("mediaman.services.arr.throttle._jitter_for", lambda dl_id, last: 1.0)

        last = 1700000000.0
        # search_count=1 → interval = 120 s. 30 s elapsed → 90 s remain → "in 1m" (floor).
        out = classify.build_search_hint(
            search_count=1,
            last_search_ts=last,
            added_at=last - 600,
            now=last + 30,
            dl_id="radarr:Foo",
        )
        assert "Searched once" in out
        assert "next attempt in 1m" in out

    def test_hours_format_band(self, monkeypatch):
        from mediaman.services.downloads.download_queue import classify

        monkeypatch.setattr("mediaman.services.arr.throttle._jitter_for", lambda dl_id, last: 1.0)

        last = 1700000000.0
        # search_count=8 → interval = 256 m = 15360 s. 1 s elapsed.
        out = classify.build_search_hint(
            search_count=8,
            last_search_ts=last,
            added_at=last - 99999,
            now=last + 1,
            dl_id="radarr:Foo",
        )
        # 15359 s ≈ 4 h 16 m → rounded to 4 h.
        assert "Searched 8×" in out
        assert "next attempt in ~4h" in out

    def test_cap_band_displays_24h(self, monkeypatch):
        from mediaman.services.downloads.download_queue import classify

        monkeypatch.setattr("mediaman.services.arr.throttle._jitter_for", lambda dl_id, last: 1.0)

        last = 1700000000.0
        out = classify.build_search_hint(
            search_count=20,
            last_search_ts=last,
            added_at=last - 99999,
            now=last + 60,
            dl_id="radarr:Foo",
        )
        assert "next attempt in ~24h" in out

    def test_firing_now_when_window_elapsed(self, monkeypatch):
        from mediaman.services.downloads.download_queue import classify

        monkeypatch.setattr("mediaman.services.arr.throttle._jitter_for", lambda dl_id, last: 1.0)

        last = 1700000000.0
        out = classify.build_search_hint(
            search_count=2,
            last_search_ts=last,
            added_at=last - 99999,
            now=last + 99999,  # well past any backoff window
            dl_id="radarr:Foo",
        )
        assert "firing now" in out

    def test_minutes_floor_minimum_one_minute(self, monkeypatch):
        """Even if there's only 5 s left, we round up to '1m' to avoid '0m'."""
        from mediaman.services.downloads.download_queue import classify

        monkeypatch.setattr("mediaman.services.arr.throttle._jitter_for", lambda dl_id, last: 1.0)

        last = 1700000000.0
        # interval(1) = 120 s. 115 s elapsed → 5 s remain.
        out = classify.build_search_hint(
            search_count=1,
            last_search_ts=last,
            added_at=last - 99999,
            now=last + 115,
            dl_id="radarr:Foo",
        )
        assert "next attempt in 1m" in out
