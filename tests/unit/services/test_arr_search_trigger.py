"""Tests for arr_search_trigger — covering state inspection and partial-missing helpers.

The throttling / trigger-on-call behaviour is already covered in
tests/unit/web/test_downloads_api.py (TestSearchTriggerThrottle and
TestTriggerPendingSearches).  This file covers the gaps: _get_search_info,
_trigger_sonarr_partial_missing, and _reset_search_triggers.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from mediaman.services.arr_search_trigger import (
    _get_search_info,
    _maybe_trigger_search,
    _reset_search_triggers,
    _trigger_sonarr_partial_missing,
)


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure a clean slate before every test in this module."""
    _reset_search_triggers()
    yield
    _reset_search_triggers()


# ---------------------------------------------------------------------------
# _get_search_info
# ---------------------------------------------------------------------------


class TestGetSearchInfo:
    def test_returns_zeros_for_unknown_id(self):
        """An id never seen before returns (0, 0.0)."""
        count, last = _get_search_info("unknown_id")
        assert count == 0
        assert last == 0.0

    def test_returns_count_after_trigger(self, monkeypatch):
        """After a search fires, _get_search_info reflects the updated count."""
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
        _maybe_trigger_search(conn, item, matched_nzb=False)

        count, last = _get_search_info("radarr:Interstellar")
        assert count > 0
        assert last > 0.0


# ---------------------------------------------------------------------------
# _reset_search_triggers
# ---------------------------------------------------------------------------


class TestResetSearchTriggers:
    def test_reset_clears_state(self, monkeypatch):
        """After triggering a search, _reset_search_triggers zeros everything out."""
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
        _maybe_trigger_search(conn, item, matched_nzb=False)

        # Confirm state was written
        count, last = _get_search_info("radarr:Arrival")
        assert count > 0

        _reset_search_triggers()

        count, last = _get_search_info("radarr:Arrival")
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
            "mediaman.services.arr_search_trigger._maybe_trigger_search",
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
            "mediaman.services.arr_search_trigger._maybe_trigger_search",
            lambda *a, **kw: calls.append(a),
        )

        # Must not raise
        _trigger_sonarr_partial_missing(conn, [])
        assert calls == []
