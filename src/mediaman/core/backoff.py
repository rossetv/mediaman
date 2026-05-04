"""Ring 0: exponential backoff with optional deterministic jitter.

This module provides a single ``ExponentialBackoff`` class that covers two
distinct use cases present in the codebase:

Plain backoff (notifications)
    ``delay(attempts)`` returns ``min(base * 2^max(n-1,0), max_seconds)``
    with no randomness — correct when a state machine advances its own
    counter and the next retry epoch is stored, so the delay value only
    needs to be computed once per failure.

Deterministic-jitter backoff (arr search throttle)
    ``delay(attempts, seed=<bytes>)`` applies a ±``jitter`` multiplicative
    factor derived from a blake2b digest of the seed rather than from the
    interpreter's random state.

    Determinism is load-bearing for the throttle.  The backoff gate is
    re-evaluated on every ``/api/downloads`` poll (potentially many times
    per minute across multiple workers).  A fresh random roll on each call
    would let a search through whenever a low multiplier was rolled,
    defeating the rate limit.  Seeding from a blake2b digest keeps the
    multiplier stable across polls and across processes — unlike ``hash()``,
    which is salted by ``PYTHONHASHSEED`` and produces different values in
    different interpreter instances.

    When ``jitter > 0`` a ``seed`` *must* be supplied; omitting it raises
    ``ValueError`` immediately rather than silently rolling a fresh random
    value that would break the invariant above.

Ring 0 contract: stdlib only (hashlib, random), no I/O, no imports from
other mediaman modules.

Canonical home: ``mediaman.core.backoff``.
Back-compat shim: ``mediaman.services.infra.backoff``.
"""

from __future__ import annotations

import hashlib
import random


class ExponentialBackoff:
    """Compute capped exponential backoff delays with optional deterministic jitter.

    Args:
        base_seconds: The delay after the first failure (attempts == 1).
        max_seconds: The hard ceiling on any returned delay.
        jitter: Fractional jitter range ``[0.0, 1.0)``.  A value of ``0.1``
            applies a ±10% multiplicative factor.  ``0.0`` (the default)
            disables jitter entirely.

    Example — plain backoff::

        backoff = ExponentialBackoff(60.0, 1800.0)
        backoff.delay(1)  # 60.0
        backoff.delay(2)  # 120.0
        backoff.delay(6)  # 1800.0 (capped)

    Example — deterministic jitter::

        backoff = ExponentialBackoff(120.0, 86400.0, jitter=0.1)
        seed = b"show-123|1234567890.0"
        backoff.delay(3, seed=seed)  # stable across polls
    """

    def __init__(
        self,
        base_seconds: float,
        max_seconds: float,
        jitter: float = 0.0,
    ) -> None:
        if jitter < 0.0 or jitter >= 1.0:
            raise ValueError(f"jitter must be in [0.0, 1.0), got {jitter!r}")
        self._base = base_seconds
        self._max = max_seconds
        self._jitter = jitter

    def delay(self, attempts: int, *, seed: bytes | None = None) -> float:
        """Return the backoff delay in seconds for *attempts* completed failures.

        Args:
            attempts: Number of failures observed so far.  The first failure
                (``attempts == 1``) returns ``base_seconds``; subsequent
                failures double until ``max_seconds`` is reached.
            seed: Byte string used to derive a deterministic jitter
                multiplier.  Required when ``jitter > 0``; ignored (and
                should be ``None``) when ``jitter == 0``.

        Raises:
            ValueError: When ``jitter > 0`` and ``seed`` is ``None``.
        """
        if self._jitter > 0.0 and seed is None:
            raise ValueError(
                "seed is required when jitter > 0 — non-deterministic jitter is never "
                "acceptable here because the delay is re-evaluated on every poll"
            )

        n = max(attempts, 0)
        base_delay = min(self._base * 2 ** max(n - 1, 0), self._max)

        if self._jitter == 0.0 or seed is None:
            return base_delay

        multiplier = self._deterministic_multiplier(seed)
        # Clamp again after applying jitter so a +jitter% roll at the cap
        # never pushes above the advertised ceiling.
        return min(base_delay * multiplier, self._max)

    def _deterministic_multiplier(self, seed: bytes) -> float:
        """Return a stable ±jitter multiplier seeded from *seed*.

        Uses blake2b (not ``hash()``) because ``PYTHONHASHSEED`` salts the
        built-in hash differently on every interpreter start — two workers
        handling the same item would compute different multipliers and
        disagree on whether the gate is open.  blake2b produces the same
        digest regardless of process, platform, or Python version.
        """
        digest = hashlib.blake2b(seed, digest_size=4).digest()
        int_seed = int.from_bytes(digest, "big")
        rng = random.Random(int_seed)
        return rng.uniform(1.0 - self._jitter, 1.0 + self._jitter)
