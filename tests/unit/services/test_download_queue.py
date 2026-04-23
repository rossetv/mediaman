"""Tests for download_queue pure helper functions."""
from __future__ import annotations

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


class TestMaybeRecordCompletionsLockDiscipline:
    """C20 — _maybe_record_completions must release the state lock
    BEFORE doing Radarr/Sonarr HTTP I/O. A hung Arr previously stalled
    every other /downloads request site-wide because the lock was held
    across the network call."""

    def test_lock_not_held_during_http(self):
        from unittest.mock import MagicMock, patch

        import mediaman.services.download_queue as dq

        dq._reset_previous_queue()

        # Seed a prior snapshot so detect_completed has something to
        # report and record_verified_completions will be invoked.
        with dq._state_lock:
            dq._previous_queue = {"radarr:Dune": {"id": "radarr:Dune", "title": "Dune", "kind": "movie", "poster_url": ""}}
            dq._previous_initialised = True

        lock_held_during_io: dict[str, bool] = {"value": True}

        def fake_record(conn, completed, build_client):
            # Attempt to acquire the lock non-blocking. If the lock is
            # currently held, this returns False. If not, it returns
            # True and we immediately release it.
            got = dq._state_lock.acquire(blocking=False)
            if got:
                dq._state_lock.release()
                lock_held_during_io["value"] = False
            else:
                lock_held_during_io["value"] = True

        with patch.object(dq, "record_verified_completions", side_effect=fake_record):
            dq._maybe_record_completions(MagicMock(), current_map={})

        assert lock_held_during_io["value"] is False, (
            "state lock was held across record_verified_completions — C20 regression"
        )

        # Cleanup
        dq._reset_previous_queue()


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
