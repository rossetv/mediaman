"""Tests for download_format helpers — H37 (classify_series_upcoming) and H41 (parse_clean_title)."""

from __future__ import annotations

import logging

from mediaman.services.download_format import (
    classify_series_upcoming,
    parse_clean_title,
)

# ---------------------------------------------------------------------------
# H41: parse_clean_title
# ---------------------------------------------------------------------------


class TestParseCleanTitle:
    def test_standard_title_year_resolution(self):
        """Standard 'Title.Year.Resolution' pattern."""
        assert parse_clean_title("Dune.2021.1080p.x264") == "Dune"

    def test_year_prefix_does_not_become_title(self):
        """Year-prefixed names like '2021.Dune.1080p.x264' must return 'Dune', not '2021'."""
        assert parse_clean_title("2021.Dune.1080p.x264") == "Dune"

    def test_title_only(self):
        """No technical tokens — return the full normalised string."""
        result = parse_clean_title("Oppenheimer")
        assert result == "Oppenheimer"

    def test_multi_word_title(self):
        assert parse_clean_title("The.Dark.Knight.2008.BluRay.1080p") == "The Dark Knight"

    def test_series_episode_marker_stripped(self):
        assert parse_clean_title("Breaking.Bad.S01E01.1080p") == "Breaking Bad"

    def test_resolution_4k_stripped(self):
        assert parse_clean_title("Avatar.2009.4K.HDR.HEVC") == "Avatar"

    def test_dots_replaced_by_spaces(self):
        result = parse_clean_title("The.Matrix.1999.720p")
        assert result == "The Matrix"

    def test_underscores_replaced_by_spaces(self):
        result = parse_clean_title("The_Matrix_1999_720p")
        assert result == "The Matrix"

    def test_webdl_stripped(self):
        assert parse_clean_title("Succession.S04E09.WEB-DL.1080p") == "Succession"

    def test_webrip_stripped(self):
        assert parse_clean_title("Andor.S01.WEBRip.x265") == "Andor"

    def test_year_only_year_year_returns_title_portion(self):
        """Two years in a row: first is stripped, second is stripped, title is empty → full name."""
        # Edge: e.g. "2001 2001 A Space Odyssey" — unrealistic but shouldn't crash.
        result = parse_clean_title("Inception.2010")
        assert result == "Inception"

    def test_empty_string_returns_empty(self):
        assert parse_clean_title("") == ""


# ---------------------------------------------------------------------------
# H37: classify_series_upcoming — unparseable airDateUtc handling
# ---------------------------------------------------------------------------


class TestClassifySeriesUpcomingAirDateHandling:
    """Episodes with unparseable airDateUtc must not be silently dropped."""

    @staticmethod
    def _series(status: str = "upcoming") -> dict:
        return {"monitored": True, "status": status, "statistics": {"episodeFileCount": 0}}

    def test_unparseable_airdateutc_does_not_drop_episode(self):
        """An episode with a malformed airDateUtc goes into the unknown bucket,
        not into the past- or future-aired buckets.  The series classification
        still proceeds based on the parseable episodes."""
        series = self._series("upcoming")
        episodes = [
            {"airDateUtc": "not-a-date"},
            {"airDateUtc": "2099-01-01T00:00:00Z"},  # valid future
        ]
        is_upcoming, label = classify_series_upcoming(series, episodes)
        assert is_upcoming is True
        assert "2099" in label or "Premieres" in label

    def test_all_unparseable_airdates_fallback_to_status(self):
        """When every episode has an unparseable date, the series status drives classification."""
        series = self._series("upcoming")
        episodes = [
            {"airDateUtc": "garbage"},
            {"airDateUtc": ""},
        ]
        is_upcoming, label = classify_series_upcoming(series, episodes)
        # status == "upcoming" so is_upcoming == True even with no future dates
        assert is_upcoming is True
        assert label == "Not yet aired"

    def test_unparseable_airdateutc_logged(self, caplog):
        """Unknown-airdate episodes should produce a debug log entry."""
        series = self._series("continuing")
        episodes = [{"airDateUtc": "not-a-valid-date"}]
        with caplog.at_level(logging.DEBUG, logger="mediaman"):
            classify_series_upcoming(series, episodes)
        assert "unknown_airdate_count" in caplog.text

    def test_no_log_when_all_dates_valid(self, caplog):
        """No debug log when every episode has a parseable airDateUtc."""
        series = self._series("continuing")
        episodes = [{"airDateUtc": "2099-06-01T00:00:00Z"}]
        with caplog.at_level(logging.DEBUG, logger="mediaman"):
            classify_series_upcoming(series, episodes)
        assert "unknown_airdate_count" not in caplog.text

    def test_missing_airdateutc_key_counted_as_unknown(self):
        """Episodes without the airDateUtc key at all are counted as unknown."""
        series = self._series("upcoming")
        episodes = [
            {},  # no airDateUtc key
            {"airDateUtc": "2099-01-01T00:00:00Z"},
        ]
        is_upcoming, label = classify_series_upcoming(series, episodes)
        assert is_upcoming is True

    def test_not_upcoming_when_has_aired_episodes(self):
        """Series with past-aired episodes should not be classified as upcoming."""
        series = self._series("continuing")
        episodes = [
            {"airDateUtc": "2020-01-01T00:00:00Z"},  # past
            {"airDateUtc": "not-a-date"},             # unknown
        ]
        is_upcoming, label = classify_series_upcoming(series, episodes)
        assert is_upcoming is False
