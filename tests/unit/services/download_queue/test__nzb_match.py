"""Tests for mediaman.services.downloads.download_queue._nzb_match."""

from __future__ import annotations

from mediaman.services.downloads.download_queue._nzb_match import nzb_matches_arr


class TestNzbMatchesArr:
    def test_exact_match(self):
        assert nzb_matches_arr("dune", ["dune"]) is True

    def test_nzb_is_substring_of_candidate(self):
        """NZB title contained within a longer candidate string → match."""
        assert nzb_matches_arr("dune", ["dune part one 2021 bluray"]) is True

    def test_candidate_is_substring_of_nzb(self):
        """Candidate title contained within a longer NZB string → match."""
        assert nzb_matches_arr("dune part one 2021 bluray", ["dune"]) is True

    def test_no_match(self):
        assert nzb_matches_arr("the batman", ["dune"]) is False

    def test_empty_candidates_list(self):
        assert nzb_matches_arr("dune", []) is False

    def test_first_candidate_matches(self):
        assert nzb_matches_arr("dune", ["dune", "batman"]) is True

    def test_second_candidate_matches(self):
        assert nzb_matches_arr("batman", ["dune", "batman"]) is True

    def test_no_partial_false_positive(self):
        """'the great' must not match 'the great escape' when the series is 'escape'."""
        # The candidate 'escape' is not in 'the great' and vice versa — no match.
        assert nzb_matches_arr("escape", ["the great"]) is False

    def test_bidirectional_match_title_with_parentheses(self):
        """Normalised title 'married at first sight au' ↔ longer NZB string."""
        nzb = "married at first sight au 2024 hdtv"
        cand = "married at first sight au"
        assert nzb_matches_arr(nzb, [cand]) is True

    def test_multiple_candidates_none_match(self):
        assert nzb_matches_arr("dune", ["batman", "inception"]) is False
