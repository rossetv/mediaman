"""Tests for the deterministic exponential backoff helper.

Covers ``_search_backoff_seconds`` and the jitter logic in
``_throttle_state._SEARCH_BACKOFF``.
"""

from __future__ import annotations

import pytest

from mediaman.services.arr.search_trigger import reset_search_triggers


@pytest.fixture(autouse=True)
def clean_state():
    """Ensure a clean slate before every test in this module."""
    reset_search_triggers()
    yield
    reset_search_triggers()


class TestSearchBackoff:
    """Unit tests for the deterministic exponential backoff helper."""

    def _no_jitter(self, monkeypatch):
        """Neutralise jitter by fixing the deterministic multiplier to 1.0."""
        from mediaman.services.arr import _throttle_state

        monkeypatch.setattr(
            _throttle_state._SEARCH_BACKOFF, "deterministic_multiplier", lambda seed: 1.0
        )

    def test_zero_count_returns_base_two_minutes(self, monkeypatch):
        """search_count=0 yields exactly 120 s when jitter is fixed at 1.0."""
        from mediaman.services.arr.search_trigger import _search_backoff_seconds

        self._no_jitter(monkeypatch)
        assert _search_backoff_seconds(0, "radarr:Foo", 0.0) == 120.0

    @pytest.mark.parametrize(
        ("count", "expected_minutes"),
        [
            (1, 2),
            (2, 4),
            (3, 8),
            (4, 16),
            (5, 32),
            (6, 64),
            (7, 128),
            (8, 256),
            (9, 512),
            (10, 1024),
        ],
    )
    def test_geometric_sequence(self, monkeypatch, count, expected_minutes):
        """The unjittered curve doubles each step from 2 m up to but excluding the cap."""
        from mediaman.services.arr.search_trigger import _search_backoff_seconds

        self._no_jitter(monkeypatch)
        assert _search_backoff_seconds(count, "radarr:Foo", 1.0) == expected_minutes * 60

    @pytest.mark.parametrize("count", [11, 12, 50, 200])
    def test_clamps_to_24h_cap(self, monkeypatch, count):
        """Above n=10 the unjittered value clamps to exactly 86_400 s."""
        from mediaman.services.arr.search_trigger import _search_backoff_seconds

        self._no_jitter(monkeypatch)
        assert _search_backoff_seconds(count, "radarr:Foo", 1.0) == 86_400.0

    def test_negative_count_treated_as_zero(self, monkeypatch):
        """Defensive: a stray negative count returns the base interval."""
        from mediaman.services.arr.search_trigger import _search_backoff_seconds

        self._no_jitter(monkeypatch)
        assert _search_backoff_seconds(-5, "radarr:Foo", 1.0) == 120.0

    def test_jitter_deterministic_per_fire(self):
        """Same seed bytes return the same multiplier across calls."""
        from mediaman.services.arr import _throttle_state

        seed = f"radarr:Foo|{1700000000.0!r}".encode()
        a = _throttle_state._SEARCH_BACKOFF.deterministic_multiplier(seed)
        b = _throttle_state._SEARCH_BACKOFF.deterministic_multiplier(seed)
        assert a == b

    def test_jitter_different_for_different_seeds(self):
        """Distinct (dl_id, last) pairs roll different multipliers (sample test)."""
        from mediaman.services.arr import _throttle_state

        seeds = [f"radarr:Item{i}|{(1700000000.0 + i)!r}".encode() for i in range(50)]
        multipliers = {_throttle_state._SEARCH_BACKOFF.deterministic_multiplier(s) for s in seeds}
        assert len(multipliers) > 30

    def test_jitter_within_band(self):
        """All multipliers stay in [0.9, 1.1] across a large sample."""
        from mediaman.services.arr import _throttle_state

        for i in range(1000):
            seed = f"radarr:Item{i}|{(1.0 + i * 7.31)!r}".encode()
            m = _throttle_state._SEARCH_BACKOFF.deterministic_multiplier(seed)
            assert 0.9 <= m <= 1.1

    def test_real_jitter_applied_to_curve(self):
        """Without monkeypatching, the returned value is within ±10% of the base."""
        from mediaman.services.arr.search_trigger import _search_backoff_seconds

        v = _search_backoff_seconds(5, "radarr:Foo", 1700000000.0)
        # n=5 → 32 m base = 1920 s. ±10% → [1728, 2112].
        assert 1728.0 <= v <= 2112.0

    def test_jitter_at_cap_never_exceeds_cap(self):
        """At n≥11 the jittered value is clamped to 86 400 s — no ~26 h surprises."""
        from mediaman.services.arr.search_trigger import _search_backoff_seconds

        for count in (11, 12, 20, 50, 200):
            v = _search_backoff_seconds(count, "radarr:Foo", 1700000000.0)
            assert v <= 86_400.0, f"count={count}: {v} > 86_400"
            # Lower bound: base is at cap and jitter can be as low as −10%.
            assert v >= 86_400.0 * 0.9, f"count={count}: {v} < 77_760"
