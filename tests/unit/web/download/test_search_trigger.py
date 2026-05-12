"""Tests for the search-trigger throttle and scheduler job.

Covers :mod:`mediaman.services.arr.search_trigger`:
- maybe_trigger_search: fires only when item is stale, released, not matched
- backoff curve advances through doubling steps
- trigger_pending_searches: iterates arr items, handles failures, partial-missing pass
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from mediaman.services.arr.search_trigger import (
    _last_search_trigger,
    maybe_trigger_search,
    reset_search_triggers,
    trigger_pending_searches,
)


class TestSearchTriggerThrottle:
    @pytest.fixture(autouse=True)
    def _reset_triggers(self):
        reset_search_triggers()

    def test_stale_released_movie_triggers_search(self, monkeypatch):
        """A monitored-no-file movie older than 5 min with no prior trigger fires MoviesSearch."""
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Feel My Voice",
            "arr_id": 42,
            "is_upcoming": False,
            "added_at": time.time() - 600,  # 10 minutes ago
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")

        mock_radarr.search_movie.assert_called_once_with(42)

    def test_second_call_within_two_minutes_does_not_trigger(self, monkeypatch):
        """After the first fire, the per-dl_id backoff gate is 2 min — anything sooner is dropped."""
        mock_radarr = MagicMock()
        conn = MagicMock()
        # Make the DB appear empty so no persisted count inflates previous_count.
        conn.execute.return_value.fetchone.return_value = None
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )
        # Pin jitter so the gate is exactly 120 s, not [108, 132].
        from mediaman.services.arr import _throttle_state as _ts

        monkeypatch.setattr(_ts._SEARCH_BACKOFF, "deterministic_multiplier", lambda seed: 1.0)

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Backoff Test",
            "arr_id": 99,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        # Fire #1 — wide-open gate.
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 1
        # Fire #2 immediately — gated by interval(1) = 120 s.
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 1

    def test_backoff_curve_advances_through_steps(self, monkeypatch):
        """Once each backoff window passes, the next call fires; doubles each step."""
        mock_radarr = MagicMock()
        conn = MagicMock()
        # Make the DB appear empty so no persisted count inflates previous_count.
        conn.execute.return_value.fetchone.return_value = None
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )
        from mediaman.services.arr import _throttle_state as _ts

        monkeypatch.setattr(_ts._SEARCH_BACKOFF, "deterministic_multiplier", lambda seed: 1.0)

        from mediaman.services.arr import search_trigger as st

        clock = [1700000000.0]
        monkeypatch.setattr(st.time, "time", lambda: clock[0])

        item = {
            "kind": "movie",
            "dl_id": "radarr:Backoff Cycle",
            "arr_id": 100,
            "is_upcoming": False,
            "added_at": clock[0] - 600,
        }
        st.maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 1

        # Advance past the 2-min interval(1) gate.
        clock[0] += 121
        st.maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 2

        # Now interval(2) = 4 min. 121 s isn't enough.
        clock[0] += 121
        st.maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 2

        # Advance past 4 min total.
        clock[0] += 240 + 1
        st.maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 3

    def test_upcoming_item_does_not_trigger_search(self, monkeypatch):
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Future Movie",
            "arr_id": 7,
            "is_upcoming": True,
            "added_at": time.time() - 99999,
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        mock_radarr.search_movie.assert_not_called()

    def test_recently_added_item_does_not_trigger_search(self, monkeypatch):
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Fresh Movie",
            "arr_id": 3,
            "is_upcoming": False,
            "added_at": time.time() - 60,  # 1 minute ago (below 5 min threshold)
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        mock_radarr.search_movie.assert_not_called()

    def test_matched_nzb_item_does_not_trigger_search(self, monkeypatch):
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Actively Downloading",
            "arr_id": 11,
            "is_upcoming": False,
            "added_at": time.time() - 9999,
        }
        maybe_trigger_search(conn, item, matched_nzb=True, secret_key="test-key")
        mock_radarr.search_movie.assert_not_called()

    def test_series_triggers_search_series(self, monkeypatch):
        mock_sonarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: mock_sonarr,
        )

        import time

        item = {
            "kind": "series",
            "dl_id": "sonarr:Some Show",
            "arr_id": 77,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        mock_sonarr.search_series.assert_called_once_with(77)

    def test_trigger_after_16_min_fires_again(self, monkeypatch):
        """After the 15-min throttle expires, a second call fires again."""
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        import time

        item = {
            "kind": "movie",
            "dl_id": "radarr:Dune",
            "arr_id": 42,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        # Rewind the stored timestamp by 16 minutes
        _last_search_trigger["radarr:Dune"] = time.time() - 16 * 60
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")
        assert mock_radarr.search_movie.call_count == 2


class TestTriggerPendingSearches:
    @pytest.fixture(autouse=True)
    def _reset_triggers(self):
        reset_search_triggers()

    def test_iterates_arr_items_and_pokes_search(self, monkeypatch):
        """Scheduler job walks every arr item and calls maybe_trigger_search."""
        conn = MagicMock()
        items = [
            {
                "kind": "movie",
                "dl_id": "radarr:A",
                "arr_id": 1,
                "is_upcoming": False,
                "added_at": 0,
            },
            {
                "kind": "series",
                "dl_id": "sonarr:B",
                "arr_id": 2,
                "is_upcoming": False,
                "added_at": 0,
            },
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.fetch_arr_queue",
            lambda c, _sk: items,
        )
        calls: list[tuple] = []
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.maybe_trigger_search",
            lambda c, i, matched_nzb, **kw: calls.append((i["dl_id"], matched_nzb)),
        )

        trigger_pending_searches(conn, secret_key="test-key")

        assert calls == [("radarr:A", False), ("sonarr:B", False)]

    def test_swallows_arr_queue_fetch_failure(self, monkeypatch):
        """If fetching the arr queue blows up, the scheduler job does not propagate."""
        conn = MagicMock()

        def boom(c, sk):
            raise requests.ConnectionError("radarr down")

        monkeypatch.setattr("mediaman.services.arr.search_trigger.fetch_arr_queue", boom)
        # Sonarr pass still runs — stub it out so the test is deterministic.
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )
        called = []
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.maybe_trigger_search",
            lambda *a, **kw: called.append(a),
        )

        trigger_pending_searches(conn, secret_key="test-key")

        assert called == []

    def test_sonarr_partial_missing_pokes_only_new_series(self, monkeypatch):
        """Series returned by Sonarr wanted/missing fire SeriesSearch unless
        already covered by the main pass."""
        conn = MagicMock()

        # Main pass surfaces one zero-file series (id=1).
        arr_items = [
            {
                "kind": "series",
                "dl_id": "sonarr:Already",
                "arr_id": 1,
                "is_upcoming": False,
                "added_at": 0,
            },
        ]
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.fetch_arr_queue",
            lambda c, _sk: arr_items,
        )

        # Sonarr client returns id=1 (dup) and id=2 (partial missing, new).
        mock_sonarr = MagicMock()
        mock_sonarr.get_missing_series.return_value = {
            1: "Already",
            2: "Partial Show",
        }
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: mock_sonarr,
        )

        calls: list[tuple] = []
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.maybe_trigger_search",
            lambda c, i, matched_nzb, **kw: calls.append((i["dl_id"], i["arr_id"])),
        )

        trigger_pending_searches(conn, secret_key="test-key")

        # One call from the main pass, one from the partial-missing pass.
        assert calls == [("sonarr:Already", 1), ("sonarr:Partial Show", 2)]

    def test_sonarr_partial_missing_skipped_when_client_missing(self, monkeypatch):
        conn = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.fetch_arr_queue",
            lambda c, _sk: [],
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )
        calls = []
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.maybe_trigger_search",
            lambda *a, **kw: calls.append(a),
        )

        trigger_pending_searches(conn, secret_key="test-key")

        assert calls == []
