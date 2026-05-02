"""Tests for download_queue pure helper functions."""

from __future__ import annotations

from mediaman.services.downloads.download_queue import build_episode_dicts, nzb_matches_arr


class TestNzbMatchesArr:
    def test_exact_match(self):
        assert nzb_matches_arr("dune", ["dune"]) is True

    def test_nzb_is_substring_of_candidate(self):
        assert nzb_matches_arr("dune", ["dune part one 2021"]) is True

    def test_candidate_is_substring_of_nzb(self):
        assert nzb_matches_arr("dune part one 2021 bluray", ["dune"]) is True

    def test_no_match(self):
        assert nzb_matches_arr("the batman", ["dune"]) is False

    def test_empty_candidates(self):
        assert nzb_matches_arr("dune", []) is False

    def test_multiple_candidates_first_matches(self):
        assert nzb_matches_arr("dune", ["dune", "batman"]) is True

    def test_multiple_candidates_second_matches(self):
        assert nzb_matches_arr("batman", ["dune", "batman"]) is True


class TestMaybeRecordCompletionsLockDiscipline:
    """C20 — _maybe_record_completions must release the state lock
    BEFORE doing Radarr/Sonarr HTTP I/O. A hung Arr previously stalled
    every other /downloads request site-wide because the lock was held
    across the network call."""

    def test_lock_not_held_during_http(self):
        from unittest.mock import MagicMock, patch

        import mediaman.services.downloads.download_queue as dq

        dq._reset_previous_queue()

        # Seed a prior snapshot so detect_completed has something to
        # report and record_verified_completions will be invoked.
        with dq._state_lock:
            dq._previous_queue = {
                "radarr:Dune": {
                    "id": "radarr:Dune",
                    "title": "Dune",
                    "kind": "movie",
                    "poster_url": "",
                }
            }
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
            dq._maybe_record_completions(MagicMock(), current_map={}, secret_key="test-key")

        assert lock_held_during_io["value"] is False, (
            "state lock was held across record_verified_completions — C20 regression"
        )

        # Cleanup
        dq._reset_previous_queue()


class TestEnrichWithTmdbIds:
    """D6 — every Arr-prefixed entry in the queue snapshot is stamped with
    its tmdb_id so detect_completed can propagate the id all the way through
    to record_verified_completions (which used to fall back to title-only
    matching for every completion)."""

    def test_radarr_entry_stamped_with_tmdb_id(self):
        from unittest.mock import MagicMock, patch

        import mediaman.services.downloads.download_queue as dq

        mock_radarr = MagicMock()
        mock_radarr.get_movies.return_value = [
            {"id": 7, "tmdbId": 438631, "title": "Dune"},
        ]

        def build_arr_client(conn, service, secret_key):
            return mock_radarr if service == "radarr" else None

        current_map: dict[str, dict[str, object]] = {
            "radarr:Dune": {
                "id": "radarr:Dune",
                "title": "Dune",
                "kind": "movie",
                "arr_id": 7,
            }
        }

        with patch(
            "mediaman.services.arr.build.build_arr_client",
            side_effect=build_arr_client,
        ):
            dq._enrich_with_tmdb_ids(MagicMock(), current_map, secret_key="x")

        assert current_map["radarr:Dune"]["tmdb_id"] == 438631

    def test_sonarr_entry_stamped_with_tmdb_id(self):
        from unittest.mock import MagicMock, patch

        import mediaman.services.downloads.download_queue as dq

        mock_sonarr = MagicMock()
        mock_sonarr.get_series.return_value = [
            {"id": 9, "tmdbId": 95057, "title": "Severance"},
        ]

        def build_arr_client(conn, service, secret_key):
            return mock_sonarr if service == "sonarr" else None

        current_map: dict[str, dict[str, object]] = {
            "sonarr:Severance": {
                "id": "sonarr:Severance",
                "title": "Severance",
                "kind": "series",
                "arr_id": 9,
            }
        }

        with patch(
            "mediaman.services.arr.build.build_arr_client",
            side_effect=build_arr_client,
        ):
            dq._enrich_with_tmdb_ids(MagicMock(), current_map, secret_key="x")

        assert current_map["sonarr:Severance"]["tmdb_id"] == 95057

    def test_no_arr_entries_skips_lookup(self):
        from unittest.mock import MagicMock, patch

        import mediaman.services.downloads.download_queue as dq

        mock_radarr = MagicMock()
        mock_radarr.get_movies.side_effect = AssertionError("no arr entries — should not call")

        def build_arr_client(conn, service, secret_key):
            return mock_radarr if service == "radarr" else None

        current_map: dict[str, dict[str, object]] = {
            "nzbget:foo": {"id": "nzbget:foo", "title": "Manual NZB", "kind": "movie"},
        }

        with patch(
            "mediaman.services.arr.build.build_arr_client",
            side_effect=build_arr_client,
        ):
            # Must not raise — lookup is skipped because no arr-prefixed entries.
            dq._enrich_with_tmdb_ids(MagicMock(), current_map, secret_key="x")
        assert "tmdb_id" not in current_map["nzbget:foo"]

    def test_lookup_failure_does_not_raise(self):
        """A network error during enrichment must not block completion detection."""
        from unittest.mock import MagicMock, patch

        import mediaman.services.downloads.download_queue as dq

        mock_radarr = MagicMock()
        mock_radarr.get_movies.side_effect = ConnectionError("boom")

        def build_arr_client(conn, service, secret_key):
            return mock_radarr if service == "radarr" else None

        current_map: dict[str, dict[str, object]] = {
            "radarr:Dune": {
                "id": "radarr:Dune",
                "title": "Dune",
                "kind": "movie",
                "arr_id": 7,
            }
        }
        with patch(
            "mediaman.services.arr.build.build_arr_client",
            side_effect=build_arr_client,
        ):
            dq._enrich_with_tmdb_ids(MagicMock(), current_map, secret_key="x")
        # Tmdb_id stays absent on failure — caller falls back to title-only matching.
        assert "tmdb_id" not in current_map["radarr:Dune"]


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
