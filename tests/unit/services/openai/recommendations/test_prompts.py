"""Tests for mediaman.services.openai.recommendations.prompts."""

from __future__ import annotations

from mediaman.services.openai.recommendations.prompts import (
    _PLEX_STRING_MAX_LEN,
    parse_recommendations,
    sanitise_plex_string,
    strip_season_suffix,
)

# ---------------------------------------------------------------------------
# strip_season_suffix
# ---------------------------------------------------------------------------


class TestStripSeasonSuffix:
    def test_colon_season_n(self):
        assert strip_season_suffix("The Boys: Season 5") == "The Boys"

    def test_hyphen_season_n(self):
        assert strip_season_suffix("The Boys - Season 5") == "The Boys"

    def test_space_season_n(self):
        assert strip_season_suffix("The Boys Season 5") == "The Boys"

    def test_colon_sn_short_form(self):
        assert strip_season_suffix("Stranger Things: S4") == "Stranger Things"

    def test_plain_title_unchanged(self):
        assert strip_season_suffix("The Mummy") == "The Mummy"

    def test_apostrophe_title_unchanged(self):
        """Titles with apostrophes must not be incorrectly stripped."""
        assert strip_season_suffix("Margo's Got Money Troubles") == "Margo's Got Money Troubles"

    def test_case_insensitive(self):
        assert strip_season_suffix("Euphoria: SEASON 3") == "Euphoria"


# ---------------------------------------------------------------------------
# sanitise_plex_string
# ---------------------------------------------------------------------------


class TestSanitisePlexString:
    def test_plain_ascii_unchanged(self):
        assert sanitise_plex_string("Oppenheimer") == "Oppenheimer"

    def test_control_chars_stripped(self):
        result = sanitise_plex_string("Dune\x00Movie")
        assert "\x00" not in result

    def test_newlines_stripped(self):
        result = sanitise_plex_string("Title\nWith\nNewline")
        assert "\n" not in result

    def test_truncated_to_max_len(self):
        long_title = "A" * (_PLEX_STRING_MAX_LEN + 50)
        result = sanitise_plex_string(long_title)
        assert len(result) <= _PLEX_STRING_MAX_LEN

    def test_unicode_letters_preserved(self):
        """Accented letters are valid (letter category) and must be kept."""
        result = sanitise_plex_string("Été")
        assert "t" in result

    def test_nfc_normalised(self):
        """Pre-composed vs decomposed forms should collapse to the same string."""
        import unicodedata

        precomposed = "é"  # single code point U+00E9
        decomposed = unicodedata.normalize("NFD", precomposed)  # e + combining acute
        assert sanitise_plex_string(precomposed) == sanitise_plex_string(decomposed)


# ---------------------------------------------------------------------------
# parse_recommendations
# ---------------------------------------------------------------------------


class TestParseRecommendations:
    def test_valid_items_normalised(self):
        items = [{"title": "Inception", "media_type": "movie", "reason": "Mind-bending."}]
        result = parse_recommendations(items, "trending")
        assert len(result) == 1
        rec = result[0]
        assert rec["title"] == "Inception"
        assert rec["media_type"] == "movie"
        assert rec["category"] == "trending"

    def test_items_without_title_skipped(self):
        items = [
            {"title": "", "media_type": "movie", "reason": "x"},
            {"title": "Dune", "media_type": "movie", "reason": "y"},
        ]
        result = parse_recommendations(items, "trending")
        assert len(result) == 1
        assert result[0]["title"] == "Dune"

    def test_unknown_media_type_defaults_to_tv(self):
        items = [{"title": "The Wire", "media_type": "series", "reason": "x"}]
        result = parse_recommendations(items, "personal")
        assert result[0]["media_type"] == "tv"

    def test_movie_media_type_preserved(self):
        items = [{"title": "Dune", "media_type": "movie", "reason": "Epic sci-fi."}]
        result = parse_recommendations(items, "trending")
        assert result[0]["media_type"] == "movie"

    def test_reason_truncated_to_max_len(self):
        """Reasons beyond the maximum length are truncated (finding 38)."""
        from mediaman.services.openai.recommendations.prompts import _LLM_REASON_MAX_LEN

        long_reason = "x" * (_LLM_REASON_MAX_LEN + 200)
        items = [{"title": "Dune", "media_type": "movie", "reason": long_reason}]
        result = parse_recommendations(items, "trending")
        assert len(result[0]["reason"]) <= _LLM_REASON_MAX_LEN

    def test_trailer_url_built(self):
        items = [{"title": "Dune", "media_type": "movie", "reason": "Great."}]
        result = parse_recommendations(items, "trending")
        assert result[0]["trailer_url"].startswith("https://www.youtube.com")

    def test_year_set_to_none(self):
        """Year is not populated by the prompt stage — enrichment does that later."""
        items = [{"title": "Dune", "media_type": "movie", "reason": "x"}]
        result = parse_recommendations(items, "trending")
        assert result[0]["year"] is None

    def test_empty_input_returns_empty(self):
        assert parse_recommendations([], "trending") == []
