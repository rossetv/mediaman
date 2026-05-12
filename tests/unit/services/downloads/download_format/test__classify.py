"""Tests for mediaman.services.downloads.download_format._classify."""

from __future__ import annotations

from mediaman.services.downloads.download_format import (
    classify_movie_upcoming,
    classify_series_upcoming,
    extract_poster_url,
    map_arr_status,
    map_episode_state,
    map_state,
)

# ---------------------------------------------------------------------------
# extract_poster_url
# ---------------------------------------------------------------------------


class TestExtractPosterUrl:
    def test_returns_remote_url_for_poster(self):
        images = [{"coverType": "poster", "remoteUrl": "https://example.com/poster.jpg"}]
        assert extract_poster_url(images) == "https://example.com/poster.jpg"

    def test_ignores_non_poster_cover_types(self):
        images = [
            {"coverType": "banner", "remoteUrl": "https://example.com/banner.jpg"},
            {"coverType": "poster", "remoteUrl": "https://example.com/poster.jpg"},
        ]
        assert extract_poster_url(images) == "https://example.com/poster.jpg"

    def test_returns_empty_string_when_no_poster(self):
        images = [{"coverType": "fanart", "remoteUrl": "https://example.com/fanart.jpg"}]
        assert extract_poster_url(images) == ""

    def test_returns_empty_string_for_none(self):
        assert extract_poster_url(None) == ""

    def test_returns_empty_string_for_empty_list(self):
        assert extract_poster_url([]) == ""

    def test_skips_poster_with_no_remote_url(self):
        images = [{"coverType": "poster"}]
        assert extract_poster_url(images) == ""


# ---------------------------------------------------------------------------
# classify_movie_upcoming
# ---------------------------------------------------------------------------


class TestClassifyMovieUpcoming:
    def _movie(self, *, monitored=True, has_file=False, is_available=False, **dates) -> dict:
        m = {
            "monitored": monitored,
            "hasFile": has_file,
            "isAvailable": is_available,
        }
        m.update(dates)
        return m

    def test_available_movie_not_upcoming(self):
        """A movie that is already available should not be upcoming."""
        is_up, label = classify_movie_upcoming(self._movie(is_available=True))
        assert is_up is False
        assert label == ""

    def test_unmonitored_movie_not_upcoming(self):
        is_up, _label = classify_movie_upcoming(self._movie(monitored=False))
        assert is_up is False

    def test_movie_with_file_not_upcoming(self):
        is_up, _label = classify_movie_upcoming(self._movie(has_file=True))
        assert is_up is False

    def test_upcoming_with_digital_release(self):
        """A future digital release should produce a 'Releases …' label."""
        is_up, label = classify_movie_upcoming(self._movie(digitalRelease="2099-12-25T00:00:00Z"))
        assert is_up is True
        assert "Releases" in label

    def test_upcoming_with_no_date_returns_not_yet_released(self):
        """Monitored, not available, no release date → 'Not yet released'."""
        is_up, label = classify_movie_upcoming(self._movie())
        assert is_up is True
        assert label == "Not yet released"

    def test_far_future_date_sentinel_treated_as_no_date(self):
        """A year-9999 sentinel must not produce a 'Releases in 7973 years' label."""
        is_up, label = classify_movie_upcoming(self._movie(physicalRelease="9999-01-01T00:00:00Z"))
        assert is_up is True
        # Should fall back to "Not yet released" because the date exceeds _MAX_FUTURE_YEARS
        assert label == "Not yet released"


# ---------------------------------------------------------------------------
# classify_series_upcoming
# ---------------------------------------------------------------------------


class TestClassifySeriesUpcoming:
    def _series(self, *, monitored=True, episode_file_count=0, status="upcoming") -> dict:
        return {
            "monitored": monitored,
            "status": status,
            "statistics": {"episodeFileCount": episode_file_count},
        }

    def test_unmonitored_series_not_upcoming(self):
        is_up, _label = classify_series_upcoming(self._series(monitored=False), [])
        assert is_up is False

    def test_series_with_files_not_upcoming(self):
        is_up, _label = classify_series_upcoming(self._series(episode_file_count=3), [])
        assert is_up is False

    def test_upcoming_series_no_episodes_returns_not_yet_aired(self):
        is_up, label = classify_series_upcoming(self._series(status="upcoming"), [])
        assert is_up is True
        assert label == "Not yet aired"

    def test_upcoming_series_future_episode_has_premieres_label(self):
        eps = [{"airDateUtc": "2099-06-01T00:00:00Z"}]
        is_up, label = classify_series_upcoming(self._series(status="upcoming"), eps)
        assert is_up is True
        assert "Premieres" in label

    def test_series_with_aired_episodes_not_upcoming(self):
        """If episodes have already aired, the series is not upcoming."""
        eps = [{"airDateUtc": "2000-01-01T00:00:00Z"}]
        is_up, _label = classify_series_upcoming(self._series(status="continuing"), eps)
        assert is_up is False

    def test_episodes_with_bad_dates_counted_in_unknown_bucket(self, caplog):
        """Episodes with unparseable airDateUtc should not raise — just log."""
        eps = [{"airDateUtc": "not-a-date"}]
        import logging

        with caplog.at_level(logging.DEBUG):
            is_up, _label = classify_series_upcoming(self._series(status="upcoming"), eps)
        # Classification should still work
        assert isinstance(is_up, bool)


# ---------------------------------------------------------------------------
# map_state
# ---------------------------------------------------------------------------


class TestMapState:
    def test_no_nzbget_match_returns_searching(self):
        assert map_state(None, has_nzbget_match=False) == "searching"

    def test_downloading_with_match(self):
        assert map_state("DOWNLOADING", has_nzbget_match=True) == "downloading"

    def test_unpacking_returns_almost_ready(self):
        assert map_state("UNPACKING", has_nzbget_match=True) == "almost_ready"

    def test_pp_state_returns_almost_ready(self):
        assert map_state("PP_PROCESS", has_nzbget_match=True) == "almost_ready"


# ---------------------------------------------------------------------------
# map_arr_status
# ---------------------------------------------------------------------------


class TestMapArrStatus:
    def test_downloading_status(self):
        assert map_arr_status("downloading") == "downloading"

    def test_completed_status(self):
        assert map_arr_status("completed") == "almost_ready"

    def test_queued_status(self):
        assert map_arr_status("queued") == "downloading"

    def test_importing_tracked_state(self):
        assert map_arr_status("queued", "importing") == "almost_ready"

    def test_unknown_status_returns_searching(self):
        assert map_arr_status("weird_state") == "searching"

    def test_empty_strings_return_searching(self):
        assert map_arr_status("") == "searching"


# ---------------------------------------------------------------------------
# map_episode_state
# ---------------------------------------------------------------------------


class TestMapEpisodeState:
    def test_full_progress_returns_ready(self):
        ep = {"progress": 100, "sizeleft": 0, "size": 500_000_000, "status": "completed"}
        assert map_episode_state(ep) == "ready"

    def test_zero_sizeleft_nonzero_size_returns_ready(self):
        ep = {"progress": 99, "sizeleft": 0, "size": 500_000_000, "status": "completed"}
        assert map_episode_state(ep) == "ready"

    def test_downloading_status_returns_downloading(self):
        ep = {"progress": 40, "sizeleft": 300, "size": 500_000_000, "status": "downloading"}
        assert map_episode_state(ep) == "downloading"

    def test_paused_status_returns_queued(self):
        ep = {"progress": 0, "sizeleft": 500, "size": 500_000_000, "status": "paused"}
        assert map_episode_state(ep) == "queued"

    def test_no_progress_no_status_returns_searching(self):
        ep = {"progress": 0, "sizeleft": 500, "size": 500_000_000, "status": ""}
        assert map_episode_state(ep) == "searching"
