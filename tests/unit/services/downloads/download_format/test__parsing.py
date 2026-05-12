"""Tests for mediaman.services.downloads.download_format._parsing."""

from __future__ import annotations

from mediaman.services.downloads.download_format import (
    format_episode_label,
    format_eta,
    format_relative_time,
    looks_like_series_nzb,
    normalise_for_match,
    parse_clean_title,
)

# ---------------------------------------------------------------------------
# looks_like_series_nzb
# ---------------------------------------------------------------------------


class TestLooksLikeSeriesNzb:
    def test_s01e01_pattern_detected(self):
        assert looks_like_series_nzb("Breaking.Bad.S01E01.720p") is True

    def test_season_only_s03_detected(self):
        assert looks_like_series_nzb("The.Wire.S03.720p") is True

    def test_movie_nzb_not_detected(self):
        assert looks_like_series_nzb("Dune.2021.1080p.BluRay") is False

    def test_empty_string_returns_false(self):
        assert looks_like_series_nzb("") is False

    def test_none_returns_false(self):
        assert looks_like_series_nzb(None) is False

    def test_case_insensitive_detection(self):
        assert looks_like_series_nzb("show.s01e05.WEB-DL") is True


# ---------------------------------------------------------------------------
# parse_clean_title
# ---------------------------------------------------------------------------


class TestParseCleanTitle:
    def test_standard_title_year_resolution(self):
        assert parse_clean_title("Dune.2021.1080p.x264") == "Dune"

    def test_year_prefix_not_returned_as_title(self):
        """Year-prefix names must not return the year as the title."""
        result = parse_clean_title("2021.Dune.1080p")
        assert result == "Dune"

    def test_multi_word_title(self):
        assert parse_clean_title("The.Dark.Knight.2008.BluRay.1080p") == "The Dark Knight"

    def test_episode_marker_stripped(self):
        assert parse_clean_title("Breaking.Bad.S01E01.720p") == "Breaking Bad"

    def test_no_tokens_returns_full_title(self):
        assert parse_clean_title("Oppenheimer") == "Oppenheimer"

    def test_underscores_normalised_to_spaces(self):
        assert parse_clean_title("The_Matrix_1999_720p") == "The Matrix"

    def test_webdl_token_stripped(self):
        assert parse_clean_title("Succession.S04E09.WEB-DL.1080p") == "Succession"

    def test_hevc_stripped(self):
        """HEVC codec token is stripped; year token is also stripped by the parser."""
        assert parse_clean_title("Dune.HEVC.x265") == "Dune"


# ---------------------------------------------------------------------------
# normalise_for_match
# ---------------------------------------------------------------------------


class TestNormaliseForMatch:
    def test_lowercases_title(self):
        assert normalise_for_match("DUNE") == "dune"

    def test_punctuation_replaced_by_space(self):
        assert normalise_for_match("married at first sight (AU)") == "married at first sight au"

    def test_multiple_separators_collapsed(self):
        assert normalise_for_match("The  Wire") == "the wire"

    def test_empty_string_returns_empty(self):
        assert normalise_for_match("") == ""

    def test_strips_leading_trailing_spaces(self):
        result = normalise_for_match("  Dune  ")
        assert result == "dune"


# ---------------------------------------------------------------------------
# format_relative_time
# ---------------------------------------------------------------------------


class TestFormatRelativeTime:
    def test_zero_timestamp_returns_empty(self):
        assert format_relative_time(0, now=1_000) == ""

    def test_negative_timestamp_returns_empty(self):
        assert format_relative_time(-1, now=1_000) == ""

    def test_just_now_for_recent(self):
        now = 1_000_000
        assert format_relative_time(now - 30, now) == "just now"

    def test_minutes_ago(self):
        now = 1_000_000
        assert format_relative_time(now - 600, now) == "10m ago"

    def test_hours_ago(self):
        now = 1_000_000
        assert format_relative_time(now - 7200, now) == "2h ago"

    def test_days_ago(self):
        now = 1_000_000
        assert format_relative_time(now - 86400, now) == "1d ago"


# ---------------------------------------------------------------------------
# format_episode_label
# ---------------------------------------------------------------------------


class TestFormatEpisodeLabel:
    def test_season_and_episode(self):
        assert format_episode_label(1, 2) == "S01E02"

    def test_season_only_when_episode_none(self):
        assert format_episode_label(3, None) == "S03"

    def test_none_season_returns_empty(self):
        assert format_episode_label(None, 5) == ""

    def test_double_digit_values_zero_padded(self):
        assert format_episode_label(10, 15) == "S10E15"


# ---------------------------------------------------------------------------
# format_eta
# ---------------------------------------------------------------------------


class TestFormatEta:
    def test_zero_rate_returns_empty(self):
        assert format_eta(remain_mb=1000, download_rate=0) == ""

    def test_zero_remaining_returns_empty(self):
        assert format_eta(remain_mb=0, download_rate=1_000_000) == ""

    def test_under_one_minute(self):
        # 1 MB remaining at 1 MB/s = 1 second
        result = format_eta(remain_mb=1, download_rate=1_048_576)
        assert "sec" in result

    def test_over_one_minute(self):
        # 60 MB at 1 MB/s = 60 s → ~1 min
        result = format_eta(remain_mb=60, download_rate=1_048_576)
        assert "min" in result

    def test_over_one_hour(self):
        # 3600 MB at 1 MB/s = 3600 s → ~1 hr
        result = format_eta(remain_mb=3600, download_rate=1_048_576)
        assert "hr" in result
