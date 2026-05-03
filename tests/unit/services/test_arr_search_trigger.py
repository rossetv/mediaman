"""Tests for arr_search_trigger — covering state inspection and partial-missing helpers.

The throttling / trigger-on-call behaviour is already covered in
tests/unit/web/test_downloads_api.py (TestSearchTriggerThrottle and
TestTriggerPendingSearches).  This file covers the gaps: get_search_info,
_trigger_sonarr_partial_missing, and reset_search_triggers.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.services.arr.search_trigger import (
    _load_throttle_from_db,
    _save_trigger_to_db,
    _trigger_sonarr_partial_missing,
    get_search_info,
    maybe_trigger_search,
    reconcile_stranded_throttle,
    reset_search_triggers,
)


def _load_last_trigger_epoch(conn, dl_id: str) -> float:
    """Test helper: pull just the epoch out of the (epoch, count) tuple."""
    return _load_throttle_from_db(conn, dl_id)[0]


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
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: mock_radarr if svc == "radarr" else None,
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
# reset_search_triggers
# ---------------------------------------------------------------------------


class TestResetSearchTriggers:
    def test_reset_clears_state(self, monkeypatch):
        """After triggering a search, reset_search_triggers zeros everything out."""
        mock_radarr = MagicMock()
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: mock_radarr if svc == "radarr" else None,
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
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: mock_sonarr if svc == "sonarr" else None,
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
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: sonarr_client if svc == "sonarr" else None,
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
        """If build_arr_client returns None for sonarr, function exits cleanly."""
        conn = MagicMock()

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: None,
        )

        calls: list = []
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.maybe_trigger_search",
            lambda *a, **kw: calls.append(a),
        )

        # Must not raise
        _trigger_sonarr_partial_missing(conn, [], "test-key")
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
    """_load_throttle_from_db and _save_trigger_to_db round-trip correctly."""

    def test_load_returns_zero_for_unknown_key(self, db_conn):
        assert _load_throttle_from_db(db_conn, "radarr:Unknown") == (0.0, 0)

    def test_save_then_load_round_trips(self, db_conn):
        epoch = 1_700_000_000.0
        _save_trigger_to_db(db_conn, "radarr:Dune", epoch, 4)
        loaded_epoch, loaded_count = _load_throttle_from_db(db_conn, "radarr:Dune")
        # Allow 1-second rounding from ISO-string conversion
        assert abs(loaded_epoch - epoch) < 1.0
        assert loaded_count == 4

    def test_save_is_idempotent(self, db_conn):
        """Saving a second time replaces the first value."""
        _save_trigger_to_db(db_conn, "radarr:Inception", 1_000_000.0, 1)
        _save_trigger_to_db(db_conn, "radarr:Inception", 2_000_000.0, 7)
        loaded_epoch, loaded_count = _load_throttle_from_db(db_conn, "radarr:Inception")
        assert abs(loaded_epoch - 2_000_000.0) < 1.0
        assert loaded_count == 7

    def test_load_returns_zero_on_locked_db(self):
        """A transient ``sqlite3.OperationalError`` (e.g. locked DB) is
        swallowed and we fall back to ``(0.0, 0)``."""
        import sqlite3

        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        assert _load_throttle_from_db(bad_conn, "radarr:X") == (0.0, 0)

    def test_load_returns_zero_on_pre_migration_db(self):
        """A missing table raises ``sqlite3.DatabaseError`` — also swallowed."""
        import sqlite3

        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.DatabaseError("no such table: arr_search_throttle")
        assert _load_throttle_from_db(bad_conn, "radarr:X") == (0.0, 0)

    def test_load_propagates_unexpected_exception(self):
        """Domain-06 #9: a non-DB exception must NOT be silently swallowed.

        Regression: the previous bare ``except Exception`` masked
        schema migration faults and parser bugs — every call returned
        ``(0.0, 0)``, which the throttle interpreted as "never
        triggered" for every dl_id, effectively disabling the throttle
        across the entire instance.
        """
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = RuntimeError("genuine bug")
        with pytest.raises(RuntimeError, match="genuine bug"):
            _load_throttle_from_db(bad_conn, "radarr:X")

    def test_save_swallows_db_error(self):
        """_save_trigger_to_db is best-effort; never raises on DB failure."""
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = Exception("write failed")
        _save_trigger_to_db(bad_conn, "radarr:X", 999.0, 1)  # must not raise

    def test_maybe_trigger_search_persists_to_db(self, db_conn, monkeypatch):
        """After maybe_trigger_search fires, the DB row is written."""
        mock_radarr = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: mock_radarr if svc == "radarr" else None,
        )
        item = {
            "kind": "movie",
            "dl_id": "radarr:Tenet",
            "arr_id": 42,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        maybe_trigger_search(db_conn, item, matched_nzb=False, secret_key="test-key")

        loaded = _load_last_trigger_epoch(db_conn, "radarr:Tenet")
        assert loaded > 0.0

    def test_cold_start_reads_db_and_throttles(self, db_conn, monkeypatch):
        """A freshly restarted process reads from the DB and respects the throttle."""
        # Simulate: trigger was saved 5 minutes ago (within throttle window)
        recent_epoch = time.time() - 60  # 1 minute ago — inside 15-min throttle
        _save_trigger_to_db(db_conn, "radarr:Tenet2", recent_epoch, 3)

        calls: list = []
        mock_radarr = MagicMock()
        mock_radarr.search_movie.side_effect = lambda _: calls.append("searched")
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: mock_radarr if svc == "radarr" else None,
        )

        item = {
            "kind": "movie",
            "dl_id": "radarr:Tenet2",
            "arr_id": 99,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        maybe_trigger_search(db_conn, item, matched_nzb=False, secret_key="test-key")
        # The DB read should have warmed the cache and blocked the search
        assert calls == []

    def test_count_survives_restart(self, db_conn, monkeypatch):
        """Search count must be restored from DB after a process restart.

        Regression: prior versions only persisted the last-triggered
        timestamp, so the in-memory count reset to 0 on every deploy and
        the "Searched N×" UI hint stayed stuck near 1 even after weeks
        of background searches.
        """
        mock_radarr = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: mock_radarr if svc == "radarr" else None,
        )

        # Simulate: process previously fired 5 searches, last one was
        # 20 minutes ago (outside the 15-min throttle, so a new search
        # may fire and bump the count).
        old_epoch = time.time() - (20 * 60)
        _save_trigger_to_db(db_conn, "radarr:LongRunner", old_epoch, 5)

        # Cold start — clear in-memory state as if the process just booted.
        reset_search_triggers()
        # `get_search_info` is now DB-backed (Wave 4-8 made the DB the
        # source of truth), so the persisted count is visible immediately
        # after the cold start, not only after a triggering call.
        warmed_count, warmed_epoch = get_search_info("radarr:LongRunner")
        assert warmed_count == 5
        assert abs(warmed_epoch - old_epoch) < 1

        item = {
            "kind": "movie",
            "dl_id": "radarr:LongRunner",
            "arr_id": 1234,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        maybe_trigger_search(db_conn, item, matched_nzb=False, secret_key="test-key")

        count, _epoch = get_search_info("radarr:LongRunner")
        assert count == 6, "expected previous 5 + 1 new trigger, not a reset to 1"

    def test_clear_throttle_removes_db_row_and_memory_state(self, db_conn, monkeypatch):
        """clear_throttle wipes the DB row and the in-memory caches."""
        from mediaman.services.arr.search_trigger import (
            _last_search_trigger,
            _search_count,
            clear_throttle,
        )

        _save_trigger_to_db(db_conn, "radarr:Tenet", 999.0, 5)
        _last_search_trigger["radarr:Tenet"] = 999.0
        _search_count["radarr:Tenet"] = 5

        clear_throttle(db_conn, "radarr:Tenet")

        # DB row gone
        epoch, count = _load_throttle_from_db(db_conn, "radarr:Tenet")
        assert (epoch, count) == (0.0, 0)
        # In-memory state cleared
        assert "radarr:Tenet" not in _last_search_trigger
        assert "radarr:Tenet" not in _search_count

    def test_clear_throttle_is_idempotent(self, db_conn):
        """Clearing a key that was never seen does not raise."""
        from mediaman.services.arr.search_trigger import clear_throttle

        clear_throttle(db_conn, "radarr:NeverExisted")  # must not raise


# ---------------------------------------------------------------------------
# Finding 25: lock is released during network I/O
# ---------------------------------------------------------------------------


class TestLockReleasedDuringNetwork:
    def test_state_lock_not_held_during_network_call(self, monkeypatch):
        """Finding 25: another thread must be able to acquire ``_state_lock``
        while ``maybe_trigger_search`` is mid-HTTP-call.

        Regression: the original implementation wrapped the entire
        Radarr/Sonarr ``search_movie`` call in ``with _state_lock``, so
        a slow upstream blocked every sibling worker's throttle read.
        With the fix the lock is reserved-then-released before the
        network call runs.
        """
        import threading

        from mediaman.services.arr import search_trigger as _st

        observed = {"lock_was_free": False}
        network_can_finish = threading.Event()
        about_to_call = threading.Event()

        def slow_search_movie(_):
            about_to_call.set()
            # Block until the assertion thread has confirmed the lock
            # is free, then return so the trigger can complete.
            network_can_finish.wait(timeout=2)

        client = MagicMock()
        client.search_movie.side_effect = slow_search_movie

        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: client if svc == "radarr" else None,
        )

        def asserter():
            about_to_call.wait(timeout=2)
            # Must be able to grab the lock while the network call is
            # in flight. ``acquire(blocking=False)`` returns True only
            # when the lock was actually free.
            observed["lock_was_free"] = _st._state_lock.acquire(blocking=False)
            if observed["lock_was_free"]:
                _st._state_lock.release()
            network_can_finish.set()

        item = {
            "kind": "movie",
            "dl_id": "radarr:LockTest",
            "arr_id": 999,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }

        t = threading.Thread(target=asserter)
        t.start()
        try:
            _st.maybe_trigger_search(MagicMock(), item, matched_nzb=False, secret_key="key")
        finally:
            t.join(timeout=2)

        assert observed["lock_was_free"], (
            "The throttle lock must not be held while the Arr HTTP call is in flight"
        )

    def test_failed_trigger_rolls_back_reservation(self, db_conn, monkeypatch):
        """When the network call fails, the throttle slot is released
        so a retry can fire on the next tick rather than waiting out
        the full 15-minute throttle window."""
        from mediaman.services.arr import search_trigger as _st

        client = MagicMock()
        client.search_movie.side_effect = RuntimeError("Radarr down")
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: client if svc == "radarr" else None,
        )

        item = {
            "kind": "movie",
            "dl_id": "radarr:RollbackMe",
            "arr_id": 1234,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        _st.maybe_trigger_search(db_conn, item, matched_nzb=False, secret_key="key")

        # The reservation should not have stuck — _last_search_trigger
        # has either been removed (no prior value) or restored.
        assert _st._last_search_trigger.get("radarr:RollbackMe", 0.0) == 0.0
        # And the count must not have been incremented.
        assert _st._search_count.get("radarr:RollbackMe", 0) == 0

    def test_rollback_uses_per_attempt_token(self, db_conn, monkeypatch):
        """Domain-06 #8: a sibling thread overwriting
        ``_last_search_trigger[dl_id]`` after our reservation must NOT
        cause our rollback to silently no-op.

        Regression: the original implementation compared
        ``_last_search_trigger.get(dl_id) == now``. A sibling thread
        racing in the few ms between our reservation and our rollback
        would write a different timestamp, and the equality check would
        skip the rollback even though the slot was emphatically NOT
        ours any more — leaving the reservation pinned to whatever the
        sibling stamped.

        With the per-attempt token, the rollback compares
        ``_reservation_tokens[dl_id] == my_token`` and correctly defers
        to the sibling's reservation rather than nuking it.
        """
        from mediaman.services.arr import search_trigger as _st

        # Failure path so the rollback branch fires.
        client = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: client if svc == "radarr" else None,
        )

        # Simulate a sibling stamping its own reservation between phase 1
        # and phase 3 by patching the network call to mutate state at
        # the moment we'd otherwise be holding the slot.
        sibling_now = time.time() + 0.001
        sibling_token = "sibling-token"

        def overwrite_then_fail(_arr_id):
            with _st._state_lock:
                _st._last_search_trigger["radarr:RaceMe"] = sibling_now
                _st._reservation_tokens["radarr:RaceMe"] = sibling_token
            raise RuntimeError("our network call failed AFTER sibling overwrote")

        client.search_movie.side_effect = overwrite_then_fail

        item = {
            "kind": "movie",
            "dl_id": "radarr:RaceMe",
            "arr_id": 9999,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        _st.maybe_trigger_search(db_conn, item, matched_nzb=False, secret_key="key")

        # Our rollback must NOT have undone the sibling's reservation.
        assert _st._last_search_trigger.get("radarr:RaceMe") == sibling_now, (
            "rollback overwrote a sibling worker's reservation"
        )
        assert _st._reservation_tokens.get("radarr:RaceMe") == sibling_token, (
            "rollback dropped the sibling's reservation token"
        )

    def test_db_read_happens_outside_state_lock(self, monkeypatch):
        """Domain-06 #7: ``_state_lock`` must NOT be held while the DB
        read for warm-up runs.

        Regression: the original implementation called
        ``_load_throttle_from_db`` inside ``with _state_lock``, so a
        single locked SQLite query for one dl_id serialised every
        sibling worker's throttle check across the entire dict — one
        slow read could starve the lot.
        """
        from mediaman.services.arr import search_trigger as _st

        observed = {"lock_held_during_db_read": False}

        def fake_load(conn, dl_id):
            observed["lock_held_during_db_read"] = _st._state_lock.locked()
            return 0.0, 0

        monkeypatch.setattr(_st, "_load_throttle_from_db", fake_load)

        # Stub the network path to return cleanly so the test only
        # measures phase 0 + phase 1.
        client = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_arr_client",
            lambda c, svc, sk: client if svc == "radarr" else None,
        )

        item = {
            "kind": "movie",
            "dl_id": "radarr:DbReadOutsideLock",
            "arr_id": 7,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        _st.maybe_trigger_search(MagicMock(), item, matched_nzb=False, secret_key="key")

        assert not observed["lock_held_during_db_read"], (
            "_load_throttle_from_db must run OUTSIDE _state_lock so a slow "
            "DB read does not block sibling workers' throttle checks."
        )


# ---------------------------------------------------------------------------
# TestReconcileStrandedThrottle (Domain-06 #10)
# ---------------------------------------------------------------------------


class TestReconcileStrandedThrottle:
    """Stranded ``arr_search_throttle`` rows are reaped after the TTL."""

    def test_returns_zero_when_table_is_empty(self, db_conn):
        """Empty table — nothing to do, no error."""
        assert reconcile_stranded_throttle(db_conn) == 0

    def test_keeps_recent_rows(self, db_conn):
        """A row triggered within the TTL window must NOT be reaped."""
        recent = time.time() - 60  # 1 minute ago
        _save_trigger_to_db(db_conn, "radarr:Active", recent, 1)
        assert reconcile_stranded_throttle(db_conn) == 0
        epoch, _ = _load_throttle_from_db(db_conn, "radarr:Active")
        assert epoch > 0

    def test_deletes_rows_older_than_ttl(self, db_conn):
        """A row whose ``last_triggered_at`` is older than the TTL is reaped.

        The default TTL is 90 days; we use a small custom TTL here so
        the test doesn't have to write a 91-day-old timestamp.
        """
        old_epoch = time.time() - 1_000  # well past 100s
        _save_trigger_to_db(db_conn, "radarr:Stranded", old_epoch, 7)

        deleted = reconcile_stranded_throttle(db_conn, ttl_seconds=100)

        assert deleted == 1
        # And the row really is gone.
        epoch, count = _load_throttle_from_db(db_conn, "radarr:Stranded")
        assert (epoch, count) == (0.0, 0)

    def test_mixed_old_and_new_only_reaps_old(self, db_conn):
        """Only stale rows are deleted; recent ones survive."""
        old_epoch = time.time() - 1_000
        recent_epoch = time.time() - 10
        _save_trigger_to_db(db_conn, "radarr:OldA", old_epoch, 1)
        _save_trigger_to_db(db_conn, "radarr:OldB", old_epoch, 2)
        _save_trigger_to_db(db_conn, "radarr:Recent", recent_epoch, 3)

        deleted = reconcile_stranded_throttle(db_conn, ttl_seconds=100)

        assert deleted == 2
        # Recent row still there.
        _epoch, count = _load_throttle_from_db(db_conn, "radarr:Recent")
        assert count == 3
        # Old rows gone.
        assert _load_throttle_from_db(db_conn, "radarr:OldA") == (0.0, 0)
        assert _load_throttle_from_db(db_conn, "radarr:OldB") == (0.0, 0)

    def test_ttl_boundary_is_strictly_older(self, db_conn):
        """A row exactly at the TTL boundary survives (strict inequality)."""
        # The boundary check is ``last_triggered_at < cutoff``, so a row
        # at the boundary itself must NOT be deleted.
        boundary_epoch = time.time() - 50
        _save_trigger_to_db(db_conn, "radarr:Boundary", boundary_epoch, 1)
        # TTL of 1000s means cutoff is 1000s ago, the row is only 50s
        # old — it must survive.
        deleted = reconcile_stranded_throttle(db_conn, ttl_seconds=1_000)
        assert deleted == 0

    def test_returns_zero_on_locked_db(self):
        """A transient DB error logs and returns 0 rather than raising."""
        import sqlite3

        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        assert reconcile_stranded_throttle(bad_conn) == 0


# ---------------------------------------------------------------------------
# TestAutoAbandon
# ---------------------------------------------------------------------------


class TestAutoAbandon:
    def test_off_when_multiplier_zero(self, db_conn, monkeypatch):
        """Default config (multiplier=0) never auto-abandons."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: 0 if k == "abandon_search_auto_multiplier" else 50,
        )
        called = {"abandon_movie": 0}
        # Patch the late-imported symbol via its source module so the local
        # import inside maybe_auto_abandon picks up our fake.
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(
            abandon_module,
            "abandon_movie",
            lambda *a, **kw: called.__setitem__("abandon_movie", called["abandon_movie"] + 1),
        )
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={"kind": "movie", "dl_id": "radarr:X", "arr_id": 1},
            search_count=99999,
        )
        assert called["abandon_movie"] == 0

    def test_fires_when_count_crosses_escalate_times_multiplier(self, db_conn, monkeypatch):
        """At escalate_at=50 and multiplier=4, fires at count >= 200."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        called = {}

        def fake_abandon_movie(conn, secret, *, arr_id, dl_id):
            called["arr_id"] = arr_id
            called["dl_id"] = dl_id

        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_movie", fake_abandon_movie)
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={"kind": "movie", "dl_id": "radarr:X", "arr_id": 42},
            search_count=200,
        )
        assert called == {"arr_id": 42, "dl_id": "radarr:X"}

    def test_does_not_fire_below_threshold(self, db_conn, monkeypatch):
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        called = {"n": 0}
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(
            abandon_module,
            "abandon_movie",
            lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
        )
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={"kind": "movie", "dl_id": "radarr:X", "arr_id": 1},
            search_count=199,
        )
        assert called["n"] == 0

    def test_series_passes_derived_seasons(self, db_conn, monkeypatch):
        """For a series item, derives season list from episodes."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        called = {}

        def fake_abandon_seasons(conn, secret, *, series_id, season_numbers, dl_id):
            called["series_id"] = series_id
            called["seasons"] = sorted(season_numbers)
            called["dl_id"] = dl_id

        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_seasons", fake_abandon_seasons)
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "series",
                "dl_id": "sonarr:X",
                "arr_id": 7,
                "episodes": [
                    {"season_number": 21},
                    {"season_number": 21},
                    {"season_number": 22},
                ],
            },
            search_count=200,
        )
        assert called == {"series_id": 7, "seasons": [21, 22], "dl_id": "sonarr:X"}

    def test_series_with_only_season_zero_episodes_skipped(self, db_conn, monkeypatch):
        """Domain-06 #12: a series whose queue rows are ALL specials
        (season 0) must NOT be auto-abandoned.

        Regression: prior versions filtered no seasons. A specials-only
        queue produced ``seasons=[0]`` and then called
        ``abandon_seasons(season_numbers=[0])``, which would unmonitor
        every special on the series. Specials are typically opt-in
        monitored separately; auto-unmonitoring them would be
        catastrophic.
        """
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        called = {"n": 0}
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(
            abandon_module,
            "abandon_seasons",
            lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
        )
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "series",
                "dl_id": "sonarr:Specials",
                "arr_id": 7,
                "episodes": [
                    {"season_number": 0},
                    {"season_number": 0},
                ],
            },
            search_count=200,
        )

        # The function should be a no-op — no abandon call AT ALL.
        assert called["n"] == 0

    def test_series_with_mixed_specials_filters_season_zero(self, db_conn, monkeypatch):
        """Mixed specials + real seasons → only the real seasons are abandoned."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        called = {}

        def fake_abandon_seasons(conn, secret, *, series_id, season_numbers, dl_id):
            called["seasons"] = sorted(season_numbers)

        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_seasons", fake_abandon_seasons)
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "series",
                "dl_id": "sonarr:Mixed",
                "arr_id": 7,
                "episodes": [
                    {"season_number": 0},  # special — must be excluded
                    {"season_number": 1},
                    {"season_number": 2},
                ],
            },
            search_count=200,
        )

        assert called["seasons"] == [1, 2]

    def test_series_with_no_episodes_skipped(self, db_conn, monkeypatch):
        """Series with empty episodes list is silently skipped (no error)."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        called = {"n": 0}
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(
            abandon_module,
            "abandon_seasons",
            lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
        )
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={"kind": "series", "dl_id": "sonarr:X", "arr_id": 7, "episodes": []},
            search_count=200,
        )
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# TestAutoAbandonAuditLog
# ---------------------------------------------------------------------------


def _read_auto_abandon_rows(conn) -> list[tuple[str, str | None, str]]:
    """Return ``(action, actor, detail)`` for every auto-abandon audit row."""
    return list(
        conn.execute(
            "SELECT action, actor, detail FROM audit_log "
            "WHERE action = 'sec:auto_abandon.fired' ORDER BY id"
        ).fetchall()
    )


class TestAutoAbandonAuditLog:
    """Finding 06 — every auto-abandon firing must leave an audit trail.

    Settings writes are admin-gated, but if those creds are compromised an
    attacker can set ``multiplier=1, escalate_at=2`` to mass-unmonitor the
    library. A per-fire ``sec:auto_abandon.fired`` row makes that attack
    detectable after the fact.
    """

    def test_movie_fire_emits_audit_row(self, db_conn, monkeypatch):
        """Firing on a movie writes one ``sec:auto_abandon.fired`` row."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_movie", lambda *a, **kw: None)
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={"kind": "movie", "dl_id": "radarr:Dune", "arr_id": 42},
            search_count=200,
        )

        rows = _read_auto_abandon_rows(db_conn)
        assert len(rows) == 1
        action, actor, detail = rows[0]
        assert action == "sec:auto_abandon.fired"
        # System-driven event — actor is the empty-string convention used
        # by login.failed et al. for unauthenticated/system events.
        assert actor == ""
        # Detail is JSON-encoded after the actor= ip= prefix.
        assert "actor=- ip=-" in detail
        assert '"dl_id":"radarr:Dune"' in detail
        assert '"arr_id":42' in detail
        assert '"service":"radarr"' in detail
        assert '"multiplier":4' in detail
        assert '"escalate_at":50' in detail
        assert '"search_count":200' in detail

    def test_series_fire_emits_audit_row_with_seasons(self, db_conn, monkeypatch):
        """Series firings record the derived season list in the detail."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_seasons", lambda *a, **kw: None)
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={
                "kind": "series",
                "dl_id": "sonarr:Foundation",
                "arr_id": 7,
                "episodes": [
                    {"season_number": 1},
                    {"season_number": 2},
                    {"season_number": 2},
                ],
            },
            search_count=200,
        )

        rows = _read_auto_abandon_rows(db_conn)
        assert len(rows) == 1
        action, actor, detail = rows[0]
        assert action == "sec:auto_abandon.fired"
        assert actor == ""
        assert '"dl_id":"sonarr:Foundation"' in detail
        assert '"service":"sonarr"' in detail
        assert '"seasons":[1,2]' in detail

    def test_no_audit_row_when_multiplier_zero(self, db_conn, monkeypatch):
        """Default-off (multiplier=0) writes no audit row, no matter the count."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: 0 if k == "abandon_search_auto_multiplier" else 50,
        )
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_movie", lambda *a, **kw: None)
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={"kind": "movie", "dl_id": "radarr:X", "arr_id": 1},
            search_count=99999,
        )

        assert _read_auto_abandon_rows(db_conn) == []

    def test_no_audit_row_below_threshold(self, db_conn, monkeypatch):
        """Below escalate_at × multiplier — gated, no row written."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_movie", lambda *a, **kw: None)
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={"kind": "movie", "dl_id": "radarr:X", "arr_id": 1},
            search_count=199,  # 1 below threshold
        )

        assert _read_auto_abandon_rows(db_conn) == []

    def test_no_audit_row_for_series_with_no_episodes(self, db_conn, monkeypatch):
        """Series skipped pre-firing (no episodes) writes no audit row."""
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        import mediaman.services.downloads.abandon as abandon_module

        monkeypatch.setattr(abandon_module, "abandon_seasons", lambda *a, **kw: None)
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        maybe_auto_abandon(
            db_conn,
            "secret",
            item={"kind": "series", "dl_id": "sonarr:X", "arr_id": 7, "episodes": []},
            search_count=200,
        )

        assert _read_auto_abandon_rows(db_conn) == []

    def test_audit_row_persists_when_abandon_call_fails(self, db_conn, monkeypatch):
        """Abandon raising must NOT prevent the audit row from landing.

        ``security_event`` writes (and commits) before the abandon call,
        so a Radarr/Sonarr outage still leaves a discoverable trail of
        what the policy decided to do.
        """
        monkeypatch.setattr(
            "mediaman.services.arr.auto_abandon.get_int_setting",
            lambda c, k, **kw: {
                "abandon_search_escalate_at": 50,
                "abandon_search_auto_multiplier": 4,
            }[k],
        )
        import mediaman.services.downloads.abandon as abandon_module

        def boom(*a, **kw):
            raise RuntimeError("radarr offline")

        monkeypatch.setattr(abandon_module, "abandon_movie", boom)
        from mediaman.services.arr.search_trigger import maybe_auto_abandon

        with pytest.raises(RuntimeError):
            maybe_auto_abandon(
                db_conn,
                "secret",
                item={"kind": "movie", "dl_id": "radarr:Y", "arr_id": 42},
                search_count=200,
            )

        rows = _read_auto_abandon_rows(db_conn)
        assert len(rows) == 1
        action, _actor, detail = rows[0]
        assert action == "sec:auto_abandon.fired"
        assert '"dl_id":"radarr:Y"' in detail


# ---------------------------------------------------------------------------
# TestSearchBackoff
# ---------------------------------------------------------------------------


class TestSearchBackoff:
    """Unit tests for the deterministic exponential backoff helper."""

    def test_zero_count_returns_base_two_minutes(self, monkeypatch):
        """search_count=0 yields exactly 120 s when jitter is fixed at 1.0."""
        from mediaman.services.arr.throttle import _search_backoff_seconds

        monkeypatch.setattr(
            "mediaman.services.arr.throttle._jitter_for", lambda dl_id, last: 1.0
        )
        assert _search_backoff_seconds(0, "radarr:Foo", 0.0) == 120.0

    @pytest.mark.parametrize(
        "count, expected_minutes",
        [(1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (6, 64), (7, 128), (8, 256), (9, 512), (10, 1024)],
    )
    def test_geometric_sequence(self, monkeypatch, count, expected_minutes):
        """The unjittered curve doubles each step from 2 m up to but excluding the cap."""
        from mediaman.services.arr.throttle import _search_backoff_seconds

        monkeypatch.setattr(
            "mediaman.services.arr.throttle._jitter_for", lambda dl_id, last: 1.0
        )
        assert _search_backoff_seconds(count, "radarr:Foo", 1.0) == expected_minutes * 60

    @pytest.mark.parametrize("count", [11, 12, 50, 200])
    def test_clamps_to_24h_cap(self, monkeypatch, count):
        """Above n=10 the unjittered value clamps to exactly 86_400 s."""
        from mediaman.services.arr.throttle import _search_backoff_seconds

        monkeypatch.setattr(
            "mediaman.services.arr.throttle._jitter_for", lambda dl_id, last: 1.0
        )
        assert _search_backoff_seconds(count, "radarr:Foo", 1.0) == 86_400.0

    def test_negative_count_treated_as_zero(self, monkeypatch):
        """Defensive: a stray negative count returns the base interval."""
        from mediaman.services.arr.throttle import _search_backoff_seconds

        monkeypatch.setattr(
            "mediaman.services.arr.throttle._jitter_for", lambda dl_id, last: 1.0
        )
        assert _search_backoff_seconds(-5, "radarr:Foo", 1.0) == 120.0

    def test_jitter_deterministic_per_fire(self):
        """Same (dl_id, last_triggered_at) returns the same multiplier across calls."""
        from mediaman.services.arr.throttle import _jitter_for

        a = _jitter_for("radarr:Foo", 1700000000.0)
        b = _jitter_for("radarr:Foo", 1700000000.0)
        assert a == b

    def test_jitter_different_for_different_seeds(self):
        """Distinct (dl_id, last) pairs roll different multipliers (sample test)."""
        from mediaman.services.arr.throttle import _jitter_for

        seeds = [(f"radarr:Item{i}", 1700000000.0 + i) for i in range(50)]
        multipliers = {_jitter_for(d, t) for d, t in seeds}
        assert len(multipliers) > 30

    def test_jitter_within_band(self):
        """All multipliers stay in [0.9, 1.1] across a large sample."""
        from mediaman.services.arr.throttle import _jitter_for

        for i in range(1000):
            m = _jitter_for(f"radarr:Item{i}", 1.0 + i * 7.31)
            assert 0.9 <= m <= 1.1

    def test_real_jitter_applied_to_curve(self):
        """Without monkeypatching, the returned value is within ±10% of the base."""
        from mediaman.services.arr.throttle import _search_backoff_seconds

        v = _search_backoff_seconds(5, "radarr:Foo", 1700000000.0)
        # n=5 → 32 m base = 1920 s. ±10% → [1728, 2112].
        assert 1728.0 <= v <= 2112.0
