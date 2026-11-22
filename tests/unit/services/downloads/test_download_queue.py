"""Tests for download_queue pure helper functions."""

from __future__ import annotations

import requests

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
    """_maybe_record_completions must release the state lock
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
            "state lock was held across record_verified_completions"
        )

        # Cleanup
        dq._reset_previous_queue()


class TestEnrichWithTmdbIds:
    """Every Arr-prefixed entry in the queue snapshot is stamped with
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

        current_map: dict[str, dict[str, object]] = {
            "radarr:Dune": {
                "id": "radarr:Dune",
                "title": "Dune",
                "kind": "movie",
                "arr_id": 7,
            }
        }

        with patch(
            "mediaman.services.arr.build.build_radarr_from_db",
            return_value=mock_radarr,
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

        current_map: dict[str, dict[str, object]] = {
            "sonarr:Severance": {
                "id": "sonarr:Severance",
                "title": "Severance",
                "kind": "series",
                "arr_id": 9,
            }
        }

        with patch(
            "mediaman.services.arr.build.build_sonarr_from_db",
            return_value=mock_sonarr,
        ):
            dq._enrich_with_tmdb_ids(MagicMock(), current_map, secret_key="x")

        assert current_map["sonarr:Severance"]["tmdb_id"] == 95057

    def test_no_arr_entries_skips_lookup(self):
        from unittest.mock import MagicMock

        import mediaman.services.downloads.download_queue as dq

        current_map: dict[str, dict[str, object]] = {
            "nzbget:foo": {"id": "nzbget:foo", "title": "Manual NZB", "kind": "movie"},
        }

        # Must not raise — lookup is skipped because no arr-prefixed entries.
        dq._enrich_with_tmdb_ids(MagicMock(), current_map, secret_key="x")
        assert "tmdb_id" not in current_map["nzbget:foo"]

    def test_lookup_failure_does_not_raise(self):
        """A network error during enrichment must not block completion detection."""
        from unittest.mock import MagicMock, patch

        import mediaman.services.downloads.download_queue as dq

        mock_radarr = MagicMock()
        mock_radarr.get_movies.side_effect = requests.ConnectionError("boom")

        current_map: dict[str, dict[str, object]] = {
            "radarr:Dune": {
                "id": "radarr:Dune",
                "title": "Dune",
                "kind": "movie",
                "arr_id": 7,
            }
        }
        with patch(
            "mediaman.services.arr.build.build_radarr_from_db",
            return_value=mock_radarr,
        ):
            dq._enrich_with_tmdb_ids(MagicMock(), current_map, secret_key="x")
        # tmdb_id stays absent on failure — caller falls back to title-only matching.
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


class TestFailedNzbgetPollKeepsSnapshot:
    def _run(self, nzb_client):
        from unittest.mock import MagicMock, patch

        import mediaman.services.downloads.download_queue as dq

        dq._reset_previous_queue()
        manual = {"id": "nzbget:1", "title": "Manual", "kind": "movie"}
        with dq._state_lock:
            dq._previous_queue = {"nzbget:1": manual}
            dq._previous_initialised = True
        recorded: list = []
        with (
            patch.object(dq, "fetch_arr_queue", return_value=[]),
            patch.object(dq, "_arr_base_urls", return_value={}),
            patch.object(dq, "build_nzbget_from_db", return_value=nzb_client),
            patch.object(dq, "_enrich_with_tmdb_ids"),
            patch.object(
                dq,
                "record_verified_completions",
                side_effect=lambda c, done, k: recorded.extend(done),
            ),
            patch.object(dq, "fetch_and_sync_recent_downloads", return_value=[]),
        ):
            dq.build_downloads_response(MagicMock(), "k")
        snapshot = dict(dq._previous_queue)
        dq._reset_previous_queue()
        return recorded, snapshot

    def test_failed_fetch_reports_no_completions_and_keeps_snapshot(self):
        from unittest.mock import MagicMock

        from mediaman.services.downloads.nzbget import NzbgetError

        client = MagicMock()
        client.get_status.side_effect = NzbgetError("boom")
        recorded, snapshot = self._run(client)
        assert recorded == []
        assert "nzbget:1" in snapshot

    def test_queue_fetch_failing_after_status_succeeds_keeps_snapshot(self):
        from unittest.mock import MagicMock

        from mediaman.services.downloads.nzbget import NzbgetError

        client = MagicMock()
        client.get_status.return_value = {"DownloadRate": 0}
        client.get_queue.side_effect = NzbgetError("boom")
        recorded, snapshot = self._run(client)
        assert recorded == []
        assert "nzbget:1" in snapshot

    def test_successful_empty_list_still_reports_completion(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.get_status.return_value = {}
        client.get_queue.return_value = []
        recorded, _ = self._run(client)
        assert [c["dl_id"] for c in recorded] == ["nzbget:1"]


class TestFailedNzbgetPollStillDiffsArrEntries:
    """Polls share the module snapshot within a test, so each test is a sequence."""

    def setup_method(self):
        import mediaman.services.downloads.download_queue as dq

        dq._reset_previous_queue()

    def teardown_method(self):
        import mediaman.services.downloads.download_queue as dq

        dq._reset_previous_queue()

    def _poll(self, arr_ids, nzb_names, *, nzb_up=True):
        """Run one poll with *arr_ids* in the arr queue and *nzb_names* in NZBGet;
        return the dl_ids recorded as completed."""
        from unittest.mock import MagicMock, patch

        import mediaman.services.downloads.download_queue as dq
        from mediaman.services.downloads.download_format import build_item
        from mediaman.services.downloads.nzbget import NzbgetError

        arr_items = [
            build_item(
                dl_id=dl_id,
                title=dl_id.split(":", 1)[1],
                media_type="movie",
                poster_url="",
                state="downloading",
                progress=10,
                eta="",
                size_done="",
                size_total="",
            )
            for dl_id in arr_ids
        ]
        client = MagicMock()
        if nzb_up:
            client.get_status.return_value = {}
            client.get_queue.return_value = [
                {"NZBName": name, "Status": "DOWNLOADING", "FileSizeMB": 10} for name in nzb_names
            ]
        else:
            client.get_status.side_effect = NzbgetError("down")
        recorded: list = []
        with (
            patch.object(dq, "fetch_arr_queue", return_value=[]),
            patch.object(dq, "_arr_base_urls", return_value={}),
            patch.object(dq, "build_nzbget_from_db", return_value=client),
            patch.object(dq, "_build_arr_items", return_value=(arr_items, [])),
            patch.object(dq, "_enrich_with_tmdb_ids"),
            patch.object(
                dq,
                "record_verified_completions",
                side_effect=lambda c, done, k: recorded.extend(done),
            ),
            patch.object(dq, "fetch_and_sync_recent_downloads", return_value=[]),
        ):
            dq.build_downloads_response(MagicMock(), "k")
        return sorted(c["dl_id"] for c in recorded)

    def test_arr_item_seen_only_during_an_outage_is_recorded(self):
        assert self._poll(["radarr:Dune"], [], nzb_up=False) == []
        assert self._poll([], [], nzb_up=False) == ["radarr:Dune"]

    def test_outage_reports_arr_items_now_and_nzb_items_once_nzbget_is_back(self):
        nzb = "Manual.Movie.2024.1080p"
        assert self._poll(["sonarr:Severance"], [nzb]) == []
        assert self._poll([], [], nzb_up=False) == ["sonarr:Severance"]
        assert self._poll([], [nzb]) == []
        assert self._poll([], []) == [nzb]
