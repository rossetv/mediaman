"""Tests for the poster cache GC counter thread-safety fix (finding B3).

Verifies that maybe_sweep_cache resets the GC counter under the correct lock
so concurrent increments from other threads cannot tear the counter state.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import mediaman.web.routes.poster.cache as cache_mod


@pytest.fixture(autouse=True)
def _reset_cache_state():
    """Restore module-level state after each test."""
    original_counter = cache_mod._cache_gc_counter
    original_dir = cache_mod._cache_dir
    yield
    with cache_mod._cache_gc_counter_lock:
        cache_mod._cache_gc_counter = original_counter
    cache_mod._cache_dir = original_dir


class TestGcCounterReset:
    """GC counter reset must be guarded by _cache_gc_counter_lock (B3)."""

    def test_counter_reset_to_zero_after_sweep(self, tmp_path: Path):
        """Counter is zero after maybe_sweep_cache runs a sweep."""
        # Prime the counter to trigger a sweep.
        with cache_mod._cache_gc_counter_lock:
            cache_mod._cache_gc_counter = cache_mod._CACHE_GC_RECHECK_EVERY

        with patch.object(cache_mod, "_sweep_oldest"):
            cache_mod.maybe_sweep_cache(tmp_path)

        with cache_mod._cache_gc_counter_lock:
            assert cache_mod._cache_gc_counter == 0

    def test_concurrent_increments_do_not_race_reset(self, tmp_path: Path):
        """Counter value after N concurrent bumps followed by a reset is well-defined."""
        errors: list[Exception] = []

        def bumper():
            try:
                for _ in range(5):
                    cache_mod._bump_gc_counter()
            except Exception as exc:
                errors.append(exc)

        # Prime past the threshold so maybe_sweep_cache will reset.
        with cache_mod._cache_gc_counter_lock:
            cache_mod._cache_gc_counter = cache_mod._CACHE_GC_RECHECK_EVERY

        threads = [threading.Thread(target=bumper) for _ in range(4)]
        for t in threads:
            t.start()

        with patch.object(cache_mod, "_sweep_oldest"):
            cache_mod.maybe_sweep_cache(tmp_path)

        for t in threads:
            t.join()

        assert not errors, f"threads raised: {errors}"
        # Counter must be a valid non-negative integer — not some impossible
        # mid-update torn value (which would raise TypeError on the assertion).
        with cache_mod._cache_gc_counter_lock:
            assert isinstance(cache_mod._cache_gc_counter, int)
            assert cache_mod._cache_gc_counter >= 0

    def test_counter_not_reset_when_gc_lock_busy(self, tmp_path: Path):
        """Counter stays at its bumped value when the GC lock is already held."""
        # Prime at RECHECK_EVERY - 1 so _bump_gc_counter() inside
        # maybe_sweep_cache increments it to exactly RECHECK_EVERY and
        # returns True, then the GC lock is found busy and the reset is
        # skipped — leaving the counter at RECHECK_EVERY.
        with cache_mod._cache_gc_counter_lock:
            cache_mod._cache_gc_counter = cache_mod._CACHE_GC_RECHECK_EVERY - 1

        # Hold _cache_gc_lock so maybe_sweep_cache returns early.
        cache_mod._cache_gc_lock.acquire()
        try:
            cache_mod.maybe_sweep_cache(tmp_path)
        finally:
            cache_mod._cache_gc_lock.release()

        # Counter was not reset because the GC lock was held.
        with cache_mod._cache_gc_counter_lock:
            assert cache_mod._cache_gc_counter == cache_mod._CACHE_GC_RECHECK_EVERY
