"""Tests for arr_search_trigger — orchestration and state-inspection helpers.

Covers:
- ``get_search_info`` (including DB cold-cache fallback)
- ``reset_search_triggers``
- ``_trigger_sonarr_partial_missing``

Throttle / persistence / backoff tests live in the sibling files:
- ``test_search_throttle.py``
- ``test_search_backoff.py``
- ``test_search_trigger_auto_abandon.py``
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.services.arr.search_trigger import (
    _parse_trigger_inputs,
    _save_trigger_to_db,
    _trigger_sonarr_partial_missing,
    get_search_info,
    maybe_trigger_search,
    reset_search_triggers,
)


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure a clean slate before every test in this module."""
    reset_search_triggers()
    yield
    reset_search_triggers()


@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(str(tmp_path / "mediaman.db"))
    yield conn
    conn.close()


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
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        item = {
            "kind": "movie",
            "dl_id": "radarr:Interstellar",
            "arr_id": 55,
            "is_upcoming": False,
            "added_at": time.time() - 600,  # stale enough to trigger
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")

        count, last = get_search_info("radarr:Interstellar")
        assert count > 0
        assert last > 0.0

    def test_falls_back_to_db_when_cache_is_cold(self, db_conn, monkeypatch):
        """A cold cache + populated DB returns the persisted values.

        Regression: prior versions only read the in-memory dicts, so under
        multi-worker deployments (or after a restart) the page flickered
        between "Searched 3×" and "Added X days ago, waiting for first
        search" as polls bounced across workers with different cached
        state.
        """
        # Populate the DB as if a sibling worker had already fired three
        # searches, then make sure our in-memory state stays empty so the
        # DB fallback is the only path that can produce a non-zero result.
        _save_trigger_to_db(db_conn, "radarr:Sicario", 1_700_000_000.0, 3)
        reset_search_triggers()
        # The in-process get_db() must hand out the same connection the
        # test populated above.
        monkeypatch.setattr("mediaman.db.get_db", lambda: db_conn)

        count, last = get_search_info("radarr:Sicario")

        assert count == 3
        # ISO-string round-trip can lose sub-second precision; allow it.
        assert abs(last - 1_700_000_000.0) < 1.0

    def test_db_fallback_warms_the_cache(self, db_conn, monkeypatch):
        """After a fallback read, subsequent calls don't re-hit the DB."""
        _save_trigger_to_db(db_conn, "radarr:Tenet", 1_700_000_000.0, 7)
        reset_search_triggers()

        calls = {"n": 0}
        real_get_db = lambda: db_conn  # noqa: E731

        def counting_get_db():
            calls["n"] += 1
            return real_get_db()

        monkeypatch.setattr("mediaman.db.get_db", counting_get_db)

        get_search_info("radarr:Tenet")  # cold — hits DB once
        get_search_info("radarr:Tenet")  # warm — must NOT hit DB
        get_search_info("radarr:Tenet")

        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# _parse_trigger_inputs — input-coercion preamble lifted out of
# maybe_trigger_search (Phase-4 decomposition)
# ---------------------------------------------------------------------------


class TestParseTriggerInputs:
    """The pre-lock guard cascade + coercion. Returns ``_TriggerInputs`` or ``None``."""

    def _good_item(self) -> dict:
        return {
            "kind": "movie",
            "dl_id": "radarr:Dune",
            "arr_id": 55,
            "is_upcoming": False,
            "added_at": time.time() - 600,  # stale enough
        }

    def test_returns_inputs_when_all_guards_pass(self):
        inputs = _parse_trigger_inputs(self._good_item(), matched_nzb=False, secret_key="k")
        assert inputs is not None
        assert inputs.dl_id == "radarr:Dune"
        assert inputs.arr_id == 55
        assert inputs.kind == "movie"
        assert inputs.service == "radarr"
        assert inputs.now > 0.0

    def test_series_maps_to_sonarr_service(self):
        item = {**self._good_item(), "kind": "series", "dl_id": "sonarr:Show"}
        inputs = _parse_trigger_inputs(item, matched_nzb=False, secret_key="k")
        assert inputs is not None
        assert inputs.service == "sonarr"

    def test_returns_none_when_upcoming(self):
        item = {**self._good_item(), "is_upcoming": True}
        assert _parse_trigger_inputs(item, matched_nzb=False, secret_key="k") is None

    def test_returns_none_when_matched_nzb(self):
        assert _parse_trigger_inputs(self._good_item(), matched_nzb=True, secret_key="k") is None

    def test_returns_none_when_secret_key_empty(self):
        assert _parse_trigger_inputs(self._good_item(), matched_nzb=False, secret_key="") is None

    def test_returns_none_when_arr_id_missing(self):
        item = {**self._good_item(), "arr_id": 0}
        assert _parse_trigger_inputs(item, matched_nzb=False, secret_key="k") is None

    def test_returns_none_when_added_too_recently(self):
        item = {**self._good_item(), "added_at": time.time()}  # just added
        assert _parse_trigger_inputs(item, matched_nzb=False, secret_key="k") is None

    def test_returns_none_when_kind_unrecognised(self):
        item = {**self._good_item(), "kind": "album"}
        assert _parse_trigger_inputs(item, matched_nzb=False, secret_key="k") is None


# ---------------------------------------------------------------------------
# reset_search_triggers
# ---------------------------------------------------------------------------


class TestResetSearchTriggers:
    def test_reset_clears_state(self, monkeypatch):
        """After triggering a search, reset_search_triggers zeros everything out."""
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

        item = {
            "kind": "movie",
            "dl_id": "radarr:Arrival",
            "arr_id": 7,
            "is_upcoming": False,
            "added_at": time.time() - 999,
        }
        maybe_trigger_search(conn, item, matched_nzb=False, secret_key="test-key")

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

        arr_items = [
            {
                "kind": "series",
                "dl_id": "sonarr:Already Covered",
                "arr_id": 10,
                "is_upcoming": False,
                "added_at": 0.0,
            }
        ]
        _trigger_sonarr_partial_missing(conn, arr_items, "test-key")

        # Only the new partial series should be poked
        assert ("sonarr:New Partial", 20) in calls
        assert not any(arr_id == 10 for _, arr_id in calls)

    def test_renamed_series_does_not_bypass_partial_missing_pass(self, db_conn, monkeypatch):
        """Domain-06 #11: a series rename mid-run must NOT cause the
        partial-missing pass to fire a fresh search.

        The previous implementation built ``dl_id = sonarr:{title}``
        from the (current) Sonarr title. After a rename the new title
        produced a fresh dl_id whose throttle had never been touched,
        so this pass fired again even though the underlying arr_id had
        just been searched on the previous tick.

        ``_trigger_sonarr_partial_missing`` now pre-filters on the
        arr-id-stable parallel throttle, which is updated by every
        successful trigger of ``maybe_trigger_search``.
        """
        from mediaman.services.arr import search_trigger as _st

        # Tick 1: trigger a search for arr_id=42 under its old title via
        # the main path so the arr-id-stable throttle gets populated.
        sonarr_client = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: sonarr_client,
        )

        item_old = {
            "kind": "series",
            "dl_id": "sonarr:Old Title",
            "arr_id": 42,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        _st.maybe_trigger_search(db_conn, item_old, matched_nzb=False, secret_key="key")
        assert sonarr_client.search_series.call_count == 1

        # Tick 2: the partial-missing pass sees the same series under a
        # different title (Sonarr renamed it). Without the parallel
        # throttle this fires a second search; with it, the pass skips.
        sonarr_client.get_missing_series.return_value = {42: "New Title"}
        _st._trigger_sonarr_partial_missing(db_conn, [], "key")

        assert sonarr_client.search_series.call_count == 1, (
            "renamed series bypassed the partial-missing throttle"
        )

    def test_no_client_returns_without_error(self, monkeypatch):
        """If the Sonarr builder returns None, the function exits cleanly."""
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        calls: list = []
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.maybe_trigger_search",
            lambda *a, **kw: calls.append(a),
        )

        # Must not raise
        _trigger_sonarr_partial_missing(conn, [], "test-key")
        assert calls == []
