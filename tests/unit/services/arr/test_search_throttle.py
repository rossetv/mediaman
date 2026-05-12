"""Tests for the DB-backed throttle persistence, concurrency, and stranded-row reaping.

Covers:
- ``_load_throttle_from_db`` / ``_save_trigger_to_db`` round-trips (H44)
- ``maybe_trigger_search`` persistence and cold-start warm-up
- Lock-release during network I/O (Finding 25)
- ``reconcile_stranded_throttle`` (Domain-06 #10)
"""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.db import init_db
from mediaman.services.arr.search_trigger import (
    _load_throttle_from_db,
    _save_trigger_to_db,
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


@pytest.fixture
def db_conn(tmp_path):
    conn = init_db(str(tmp_path / "mediaman.db"))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# H44: DB-backed throttle persistence
# ---------------------------------------------------------------------------


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
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        assert _load_throttle_from_db(bad_conn, "radarr:X") == (0.0, 0)

    def test_load_returns_zero_on_pre_migration_db(self):
        """A missing table raises ``sqlite3.DatabaseError`` — also swallowed."""
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.DatabaseError(
            "no such table: arr_search_throttle"
        )
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
        """_save_trigger_to_db is best-effort; never raises on transient DB failure."""
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("write failed")
        _save_trigger_to_db(bad_conn, "radarr:X", 999.0, 1)  # must not raise

    def test_maybe_trigger_search_persists_to_db(self, db_conn, monkeypatch):
        """After maybe_trigger_search fires, the DB row is written."""
        mock_radarr = MagicMock()
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
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
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

        Regression: prior versions only persisted the timestamp, so count
        reset to 0 on every deploy — the "Searched N×" hint stayed near 1.
        """
        mock_radarr = MagicMock()
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: mock_radarr,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        # count=5, last 90 min ago — backoff at count=5 is 32 min max, so 90 min clears it.
        old_epoch = time.time() - (90 * 60)
        _save_trigger_to_db(db_conn, "radarr:LongRunner", old_epoch, 5)

        # Cold start: clear in-memory state as if the process just booted.
        reset_search_triggers()
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
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: client,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
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
        client.search_movie.side_effect = requests.ConnectionError("Radarr down")
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: client,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        item = {
            "kind": "movie",
            "dl_id": "radarr:RollbackMe",
            "arr_id": 1234,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        _st.maybe_trigger_search(db_conn, item, matched_nzb=False, secret_key="key")

        assert _st._last_search_trigger.get("radarr:RollbackMe", 0.0) == 0.0
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
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: client,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
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
            raise requests.ConnectionError("our network call failed AFTER sibling overwrote")

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
        """Domain-06 #7: ``_state_lock`` must NOT be held during the cold-cache
        DB read — a slow SQLite query must not serialise every sibling worker.
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
            "mediaman.services.arr.search_trigger.build_radarr_from_db",
            lambda c, sk: client,
        )
        monkeypatch.setattr(
            "mediaman.services.arr.search_trigger.build_sonarr_from_db",
            lambda c, sk: None,
        )

        item = {
            "kind": "movie",
            "dl_id": "radarr:DbReadOutsideLock",
            "arr_id": 7,
            "is_upcoming": False,
            "added_at": time.time() - 600,
        }
        _st.maybe_trigger_search(MagicMock(), item, matched_nzb=False, secret_key="key")

        assert not observed["lock_held_during_db_read"]


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
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        assert reconcile_stranded_throttle(bad_conn) == 0
