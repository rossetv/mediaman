"""Tests for download_queue pure helper functions."""
from __future__ import annotations
import pytest
from mediaman.services.download_queue import _nzb_matches_arr, build_episode_dicts


class TestNzbMatchesArr:
    def test_exact_match(self):
        assert _nzb_matches_arr("dune", ["dune"]) is True

    def test_nzb_is_substring_of_candidate(self):
        assert _nzb_matches_arr("dune", ["dune part one 2021"]) is True

    def test_candidate_is_substring_of_nzb(self):
        assert _nzb_matches_arr("dune part one 2021 bluray", ["dune"]) is True

    def test_no_match(self):
        assert _nzb_matches_arr("the batman", ["dune"]) is False

    def test_empty_candidates(self):
        assert _nzb_matches_arr("dune", []) is False

    def test_multiple_candidates_first_matches(self):
        assert _nzb_matches_arr("dune", ["dune", "batman"]) is True

    def test_multiple_candidates_second_matches(self):
        assert _nzb_matches_arr("batman", ["dune", "batman"]) is True


class TestBuildEpisodeDicts:
    def test_maps_fields_correctly(self):
        eps = [{"label": "S01E01", "title": "Pilot", "progress": 50, "is_pack_episode": False}]
        result = build_episode_dicts(eps)
        assert len(result) == 1
        assert result[0]["label"] == "S01E01"
        assert result[0]["title"] == "Pilot"
        assert result[0]["progress"] == 50
        assert result[0]["is_pack_episode"] is False

    def test_state_mapped(self):
        from mediaman.services.download_format import map_episode_state
        eps = [{"label": "S01E01", "title": "Ep", "state": "downloading"}]
        result = build_episode_dicts(eps)
        assert "state" in result[0]

    def test_empty_list(self):
        assert build_episode_dicts([]) == []

    def test_missing_optional_fields_use_defaults(self):
        result = build_episode_dicts([{}])
        assert result[0]["label"] == ""
        assert result[0]["title"] == ""
        assert result[0]["progress"] == 0
        assert result[0]["is_pack_episode"] is False
