"""Tests for arr_search_trigger — covering state inspection and partial-missing helpers.

The throttling / trigger-on-call behaviour is already covered in
tests/unit/web/test_downloads_api.py (TestSearchTriggerThrottle and
TestTriggerPendingSearches).  This file covers the gaps: get_search_info,
_trigger_sonarr_partial_missing, and reset_search_triggers.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.services.arr_search_trigger import (
    _load_last_trigger_from_db,
    _save_trigger_to_db,
    get_search_info,
    maybe_trigger_search,
    reset_search_triggers,
    _trigger_sonarr_partial_missing,
)


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure a clean slate before every test in this module."""
    reset_search_triggers()
    yield
    reset_search_triggers()


# ---------------------------------------------------------------------------
# get_search_info
# ---------------------------------------------------------------------------


class TestGetSearchInfo:
    def test_returns_zeros_for_unknown_id(self):
        """An id never seen before returns (0, 0.0)."""
        count, last = get_search_info("unknown_id")
        assert count == 0
        assert last == 0.0

    def test_returns_count_after_trigger(self, monkeypatch):
        """After a search fires, get_search_info reflects the updated count."""
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr_search_trigger.build_arr_client",
            lambda c, svc: mock_radarr if svc == "radarr" else None,
        )

        item = {
            "kind": "movie",
            "dl_id": "radarr:Interstellar",
            "arr_id": 55,
            "is_upcoming": False,
            "added_at": time.time() - 600,  # stale enough to trigger
        }
        maybe_trigger_search(conn, item, matched_nzb=False)

        count, last = get_search_info("radarr:Interstellar")
        assert count > 0
        assert last > 0.0


# ---------------------------------------------------------------------------
# reset_search_triggers
# ---------------------------------------------------------------------------


class TestResetSearchTriggers:
    def test_reset_clears_state(self, monkeypatch):
        """After triggering a search, reset_search_triggers zeros everything out."""
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr_search_trigger.build_arr_client",
            lambda c, svc: mock_radarr if svc == "radarr" else None,
        )

        item = {
            "kind": "movie",
            "dl_id": "radarr:Arrival",
            "arr_id": 7,
            "is_upcoming": False,
            "added_at": time.time() - 999,
        }
        maybe_trigger_search(conn, item, matched_nzb=False)

        # Confirm state was written
        count, last = get_search_info("radarr:Arrival")
        assert count > 0

        reset_search_triggers()

        count, last = get_search_info("radarr:Arrival")
        assert count == 0
        assert last == 0.0


# ---------------------------------------------------------------------------
# _trigger_sonarr_partial_missing
# ---------------------------------------------------------------------------


class TestTriggerSonarrPartialMissing:
    def test_skips_series_already_in_arr_items(self, monkeypatch):
        """Series already present in arr_items must not be poked a second time."""
        conn = MagicMock()
        mock_sonarr = MagicMock()
        # Both id=10 (already poked) and id=20 (new) returned by Sonarr
        mock_sonarr.get_missing_series.return_value = {
            10: "Already Covered",
            20: "New Partial",
        }

        monkeypatch.setattr(
            "mediaman.services.arr_search_trigger.build_arr_client",
            lambda c, svc: mock_sonarr if svc == "sonarr" else None,
        )

        calls: list[tuple] = []
        monkeypatch.setattr(
            "mediaman.services.arr_search_trigger.maybe_trigger_search",
            lambda c, i, matched_nzb: calls.append((i["dl_id"], i["arr_id"])),
        )

        arr_items = [
            {
                "kind": "series",
                "dl_id": "sonarr:Already Covered",
                "arr_id": 10,
                "is_upcoming": False,
                "added_at": 0.0,
            }
        ]
        _trigger_sonarr_partial_missing(conn, arr_items)

        # Only the new partial series should be poked
        assert ("sonarr:New Partial", 20) in calls
        assert not any(arr_id == 10 for _, arr_id in calls)

    def test_no_client_returns_without_error(self, monkeypatch):
        """If build_arr_client returns None for sonarr, function exits cleanly."""
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr_search_trigger.build_arr_client",
            lambda c, svc: None,
        )

        calls: list = []
        monkeypatch.setattr(
            "mediaman.services.arr_search_trigger.maybe_trigger_search",
            lambda *a, **kw: calls.append(a),
        )

        # Must not raise
        _trigger_sonarr_partial_missing(conn, [])
        assert calls == []


# ---------------------------------------------------------------------------
# H44: DB-backed throttle persistence
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(str(tmp_path / "mediaman.db"))
    yield conn
    conn.close()


class TestThrottleDbPersistence:
    """_load_last_trigger_from_db and _save_trigger_to_db round-trip correctly."""

    def test_load_returns_zero_for_unknown_key(self, db_conn):
        assert _load_last_trigger_from_db(db_conn, "radarr:Unknown") == 0.0

    def test_save_then_load_round_trips(self, db_conn):
        epoch = 1_700_000_000.0
        _save_trigger_to_db(db_conn, "radarr:Dune", epoch)
        loaded = _load_last_trigger_from_db(db_conn, "radarr:Dune")
        # Allow 1-second rounding from ISO-string conversion
        assert abs(loaded - epoch) < 1.0

    def test_save_is_idempotent(self, db_conn):
        """Saving a second time replaces the first value."""
        _save_trigger_to_db(db_conn, "radarr:Inception", 1_000_000.0)
        _save_trigger_to_db(db_conn, "radarr:Inception", 2_000_000.0)
        loaded = _load_last_trigger_from_db(db_conn, "radarr:Inception")
        assert abs(loaded - 2_000_000.0) < 1.0

    def test_load_returns_zero_on_broken_db(self):
        """A broken/missing DB never raises — returns 0.0 gracefully."""
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = Exception("DB locked")
        assert _load_last_trigger_from_db(bad_conn, "radarr:X") == 0.0

    def test_save_swallows_db_error(self):
        """_save_trigger_to_db is best-effort; never raises on DB failure."""
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = Exception("write failed")
        _save_trigger_to_db(bad_conn, "radarr:X", 999.0)  # must not raise

    def test_maybe_trigger_search_persists_to_db(self, db_conn, monkeypatch):
        """After maybe_trigger_search fires, the DB row is written."""
        mock_radarr = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.arr_search_trigger.build_arr_client",
            lambda c, svc: mock_radarr if svc == "radarr" else None,
        )
        item = {
            "kind": "movie",
            "dl_id": "radarr:Tenet",
            "arr_id": 42,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        maybe_trigger_search(db_conn, item, matched_nzb=False)

        loaded = _load_last_trigger_from_db(db_conn, "radarr:Tenet")
        assert loaded > 0.0

    def test_cold_start_reads_db_and_throttles(self, db_conn, monkeypatch):
        """A freshly restarted process reads from the DB and respects the throttle."""
        # Simulate: trigger was saved 5 minutes ago (within throttle window)
        recent_epoch = time.time() - 60  # 1 minute ago — inside 15-min throttle
        _save_trigger_to_db(db_conn, "radarr:Tenet2", recent_epoch)

        calls: list = []
        mock_radarr = MagicMock()
        mock_radarr.search_movie.side_effect = lambda _: calls.append("searched")
        monkeypatch.setattr(
            "mediaman.services.arr_search_trigger.build_arr_client",
            lambda c, svc: mock_radarr if svc == "radarr" else None,
        )

        item = {
            "kind": "movie",
            "dl_id": "radarr:Tenet2",
            "arr_id": 99,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        maybe_trigger_search(db_conn, item, matched_nzb=False)
        # The DB read should have warmed the cache and blocked the search
        assert calls == []
