"""Tests for mediaman.services.openai.recommendations.prompts."""

from __future__ import annotations

import json

import pytest

from mediaman.services.openai.recommendations import prompts
from mediaman.services.openai.recommendations.prompts import (
    _PLEX_STRING_MAX_LEN,
    parse_recommendations,
    sanitise_plex_string,
    strip_season_suffix,
)


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
        from mediaman.services.openai.recommendations.prompts import _LLM_REASON_MAX_LEN

        long_reason = "x" * (_LLM_REASON_MAX_LEN + 200)
        items = [{"title": "Dune", "media_type": "movie", "reason": long_reason}]
        result = parse_recommendations(items, "trending")
        assert len(result[0]["reason"]) <= _LLM_REASON_MAX_LEN

    def test_trailer_url_built(self):
        items = [{"title": "Dune", "media_type": "movie", "reason": "Great."}]
        result = parse_recommendations(items, "trending")
        assert result[0]["trailer_url"].startswith("https://www.youtube.com")

    @pytest.mark.parametrize(
        ("year", "kept"), [(1999, 1999), ("1999", 1999), ("soon", None), (3000, None), (None, None)]
    )
    def test_only_a_plausible_year_is_kept(self, year, kept):
        items = [{"title": "Dune", "media_type": "movie", "reason": "x", "year": year}]
        assert parse_recommendations(items, "trending")[0]["year"] == kept

    def test_empty_input_returns_empty(self):
        assert parse_recommendations([], "trending") == []


def _reason(reason: str, **kwargs) -> str:
    [pick] = parse_recommendations(
        [{"title": "Dune", "media_type": "movie", "reason": reason}], "personal", **kwargs
    )
    return pick["reason"]


class TestReasons:
    @pytest.mark.parametrize(
        "reason",
        [
            "Because of its pace.",
            "A heist you will like.",
            "Made for your evenings.",
            "See https://a.example/x.",
            "See www.a.example.",
            "More at films.com today.",
            "A [heist](x) story.",
            "A **bold** heist.",
            "# A heist",
            "A thief like those in Heat plans one job.",
        ],
    )
    def test_a_reason_that_may_not_be_shown_is_blanked_and_the_pick_kept(self, reason):
        assert _reason(reason, sent_titles=["Heat"]) == ""

    @pytest.mark.parametrize(
        "reason", ["A heat wave traps four strangers.", "Four thieves in Heathrow at night."]
    )
    def test_a_title_inside_another_word_or_in_small_letters_names_nothing(self, reason):
        assert _reason(reason, sent_titles=["Heat"]) == reason

    def test_a_long_reason_ends_at_a_whole_sentence(self):
        first = "A thief trains three strangers for one job."
        assert _reason(f"{first} {'And so on ' * 30}.") == first

    def test_a_long_reason_of_one_sentence_is_blanked(self):
        assert _reason("word " * 60) == ""


def _answering(monkeypatch, items=()):
    """Answer every call with *items*; returns the calls as (prompt, web search asked for)."""
    calls: list[tuple[str, bool]] = []

    def fake(prompt, conn, use_web_search=False, *, secret_key=None):
        calls.append((prompt, use_web_search))
        return list(items)

    monkeypatch.setattr(prompts, "call_openai", fake)
    return calls


class TestPersonalPrompt:
    def test_a_pick_names_at_most_two_of_the_titles_sent_and_only_those(self, monkeypatch):
        raw = {"title": "Pick", "media_type": "movie", "reason": "A heist."}
        _answering(
            monkeypatch,
            [
                {**raw, "because_of": ["H2", "H9", "H2", 7, "Secret", "H1", "H3"]},
                {**raw, "because_of": "H1"},
            ],
        )
        history = [{"title": t, "type": "movie"} for t in ("Alpha", "Beta", "Gamma")]

        first, second = prompts.generate_personal(None, history)

        assert first["because_of"] == ["Beta", "Alpha"]
        assert second["because_of"] == []

    def test_a_title_left_out_for_lack_of_room_cannot_be_named(self, monkeypatch):
        calls = _answering(
            monkeypatch,
            [{"title": "P", "media_type": "tv", "reason": "", "because_of": ["H1", "H50"]}],
        )
        history = [{"title": f"{n:02} " + "é" * 110, "type": "movie"} for n in range(50)]

        [pick] = prompts.generate_personal(None, history, previous_titles=["Old " * 20] * 100)

        prompt = calls[0][0]
        data = prompt[prompt.rindex("<BEGIN_PLEX_DATA>") : prompt.rindex("<END_PLEX_DATA>")]
        lines = [line for line in data.splitlines() if line.startswith("H")][1:]
        assert 10 < len(lines) < 50
        assert all(line.endswith("é (movie)") for line in lines)
        assert len(data.encode()) < prompts._PLEX_BLOCK_MAX_BYTES + 100
        block = data.split("<UNTRUSTED_PREVIOUS_TITLES>\n")[1].split("\n</")[0]
        assert set(json.loads(block)) == {("Old " * 20).strip()}
        assert len(pick["because_of"]) == 1

    @pytest.mark.parametrize("field", ["title", "reason"])
    def test_a_lone_surrogate_never_leaves_the_parser(self, field):
        item = {"title": "Dune", "media_type": "movie", "reason": "Sand.", field: "a\ud800b"}
        picks = parse_recommendations([item], "trending")
        assert [p["reason"] for p in picks] == ([] if field == "title" else [""])

    def test_no_more_picks_are_taken_than_were_asked_for(self):
        items = [{"title": f"Film {n}", "media_type": "movie"} for n in range(3000)]
        assert len(parse_recommendations(items, "trending")) == 20

    def test_an_earlier_title_is_sanitised_before_it_is_sent(self, monkeypatch):
        calls = _answering(monkeypatch)
        bad = "Old\u202e Film\x00 \ud800<b>"
        prompts.generate_personal(None, [{"title": "A", "type": "movie"}], None, [bad])
        prompts.generate_trending(None, [bad])
        for prompt, _web in calls:
            assert '["Old Film b"]' in prompt

    def test_a_reason_naming_a_rated_or_earlier_title_is_blanked(self, monkeypatch):
        item = {"title": "P", "media_type": "tv"}
        _answering(
            monkeypatch, [{**item, "reason": "Like Heat."}, {**item, "reason": "Like Ronin."}]
        )
        picks = prompts.generate_personal(
            None,
            [{"title": "Alpha", "type": "movie"}],
            [{"title": "Heat", "type": "movie", "stars": 5.0}],
            ["Ronin"],
        )
        assert [p["reason"] for p in picks] == ["", ""]

    def test_a_persons_titles_never_go_with_a_web_search(self, monkeypatch):
        calls = _answering(monkeypatch)
        prompts.generate_personal(None, [{"title": "Alpha", "type": "movie"}])
        assert [web for _, web in calls] == [False]


class TestTrendingPrompt:
    @pytest.mark.parametrize("web", [True, False])
    def test_the_model_is_told_to_search_only_when_it_can(self, monkeypatch, web):
        calls = _answering(monkeypatch)
        monkeypatch.setattr(prompts, "is_web_search_enabled", lambda _conn: web)
        prompts.generate_trending(None)
        assert ("Search the web" in calls[0][0]) is web
        assert ("You cannot search the web" in calls[0][0]) is not web
