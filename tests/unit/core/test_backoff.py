"""Unit tests for :mod:`mediaman.services.infra.backoff`.

Covers plain growth, capped growth, deterministic jitter (same seed →
same multiplier), and the ValueError guard that prevents non-deterministic
jitter from slipping through.
"""

from __future__ import annotations

import pytest

from mediaman.core.backoff import ExponentialBackoff


class TestPlainBackoff:
    """Plain exponential backoff — no jitter."""

    @pytest.fixture(autouse=True)
    def _setup(self, request):
        request.instance.b = ExponentialBackoff(base_seconds=60.0, max_seconds=1800.0)

    def test_first_attempt_returns_base(self):
        assert self.b.delay(1) == 60.0

    def test_second_attempt_doubles(self):
        assert self.b.delay(2) == 120.0

    def test_third_attempt_doubles_again(self):
        assert self.b.delay(3) == 240.0

    def test_growth_is_capped(self):
        # 60 * 2^5 = 1920 > 1800 — must be clamped.
        assert self.b.delay(6) == 1800.0

    def test_very_high_attempt_stays_at_cap(self):
        assert self.b.delay(100) == 1800.0

    def test_zero_attempts_returns_base(self):
        # n=0 → 2^max(0-1,0) = 2^0 = 1 → base * 1 = base.
        assert self.b.delay(0) == 60.0

    def test_seed_ignored_when_no_jitter(self):
        # Passing a seed when jitter=0 must not raise and must return the
        # plain value (seed is simply ignored).
        result = self.b.delay(2, seed=b"anything")
        assert result == 120.0


class TestCappedGrowth:
    """Verify the cap applies at various base/max combinations."""

    def test_cap_at_one_step(self):
        b = ExponentialBackoff(base_seconds=100.0, max_seconds=100.0)
        for n in range(1, 10):
            assert b.delay(n) == 100.0

    def test_cap_matches_exact_power(self):
        # base=10, max=80: 10*2^3=80 — should hit cap at attempt 4.
        b = ExponentialBackoff(base_seconds=10.0, max_seconds=80.0)
        assert b.delay(4) == 80.0
        assert b.delay(5) == 80.0


class TestDeterministicJitter:
    """Deterministic jitter — same seed must always produce the same delay."""

    @pytest.fixture(autouse=True)
    def _setup(self, request):
        request.instance.b = ExponentialBackoff(
            base_seconds=120.0,
            max_seconds=86_400.0,
            jitter=0.1,
        )
        request.instance.seed = b"show-123|1234567890.0"

    def test_same_seed_same_result(self):
        d1 = self.b.delay(3, seed=self.seed)
        d2 = self.b.delay(3, seed=self.seed)
        assert d1 == d2

    def test_different_seeds_can_differ(self):
        d1 = self.b.delay(3, seed=b"seed-A|1.0")
        d2 = self.b.delay(3, seed=b"seed-B|1.0")
        # Not guaranteed to differ, but extremely unlikely with blake2b.
        # If this ever fails spuriously it means the hash collided — just
        # pick a different pair of seeds.
        assert d1 != d2

    def test_result_is_within_jitter_range(self):
        # For attempts=1, base delay = 120s. With ±10% jitter:
        # result must be in [108, 132].
        d = self.b.delay(1, seed=self.seed)
        assert 108.0 <= d <= 132.0

    def test_jitter_does_not_exceed_cap(self):
        # At the cap (86 400s), +10% = 95 040 > cap — must stay at 86 400.
        b = ExponentialBackoff(base_seconds=120.0, max_seconds=86_400.0, jitter=0.1)
        # Force the base delay to hit the cap by using a huge attempt count.
        d = b.delay(100, seed=b"any-seed")
        assert d <= 86_400.0

    def test_stable_across_multiple_calls(self):
        results = [self.b.delay(5, seed=self.seed) for _ in range(20)]
        assert len(set(results)) == 1, "All calls with same seed must return identical value"


class TestValueErrorOnMissingSeed:
    """ValueError must be raised when jitter > 0 and seed is omitted."""

    def test_raises_when_seed_none(self):
        b = ExponentialBackoff(base_seconds=60.0, max_seconds=3600.0, jitter=0.1)
        with pytest.raises(ValueError, match="seed is required"):
            b.delay(1)

    def test_no_error_when_seed_provided(self):
        b = ExponentialBackoff(base_seconds=60.0, max_seconds=3600.0, jitter=0.1)
        result = b.delay(1, seed=b"ok")
        assert result > 0

    def test_no_error_when_jitter_zero_and_no_seed(self):
        b = ExponentialBackoff(base_seconds=60.0, max_seconds=3600.0, jitter=0.0)
        result = b.delay(2)
        assert result == 120.0


class TestConstructorValidation:
    """ExponentialBackoff rejects invalid jitter values at construction time."""

    def test_negative_jitter_raises(self):
        with pytest.raises(ValueError):
            ExponentialBackoff(60.0, 3600.0, jitter=-0.1)

    def test_jitter_of_one_raises(self):
        with pytest.raises(ValueError):
            ExponentialBackoff(60.0, 3600.0, jitter=1.0)

    def test_jitter_just_below_one_is_valid(self):
        # 0.99 is a legitimate (if extreme) jitter value.
        b = ExponentialBackoff(60.0, 3600.0, jitter=0.99)
        assert b.delay(1, seed=b"x") > 0
