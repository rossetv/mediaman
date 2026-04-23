"""Sliding-window rate limiters.

Split from the original monolithic ``rate_limit.py`` (R4). Holds
:class:`RateLimiter` (IP-bucketed) and :class:`ActionRateLimiter`
(per-actor, for authenticated admin operations).
"""

from __future__ import annotations

import ipaddress
import threading
import time

# Cap the per-IP rate-limit dict to prevent unbounded memory growth under
# a distributed attack. Oldest entries are evicted when the cap is hit.
_MAX_BUCKETS = 10_000


class ActionRateLimiter:
    """Per-actor rate limiter for authenticated admin operations.

    Keyed on username. Thread-safe; enforces both a short burst window
    and a daily cap.
    """

    def __init__(self, max_in_window: int, window_seconds: float, max_per_day: int = 0):
        self._max_in_window = max_in_window
        self._window = window_seconds
        self._max_per_day = max_per_day
        self._attempts: dict[str, list[float]] = {}
        self._day_counts: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()
        self._calls_since_prune = 0

    _DAY_COUNT_PRUNE_EVERY = 512

    def check(self, actor: str) -> bool:
        """Return True if the action is allowed, False if rate-limited."""
        now = time.monotonic()
        today = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            if self._max_per_day > 0:
                day_key, day_count = self._day_counts.get(actor, ("", 0))
                if day_key != today:
                    day_count = 0
                if day_count >= self._max_per_day:
                    return False

            attempts = [t for t in self._attempts.get(actor, []) if now - t < self._window]
            if len(attempts) >= self._max_in_window:
                self._attempts[actor] = attempts
                return False

            if self._max_per_day > 0:
                day_key, day_count = self._day_counts.get(actor, ("", 0))
                if day_key != today:
                    day_count = 0
                self._day_counts[actor] = (today, day_count + 1)
                self._calls_since_prune += 1
                if self._calls_since_prune >= self._DAY_COUNT_PRUNE_EVERY:
                    self._calls_since_prune = 0
                    stale_actors = [
                        k for k, (dk, _) in self._day_counts.items() if dk != today
                    ]
                    for k in stale_actors:
                        self._day_counts.pop(k, None)

            attempts.append(now)
            self._attempts[actor] = attempts
            return True


def _bucket_key(ip: str) -> str:
    """Collapse an IP into a network-prefix bucket key (IPv4 /24, IPv6 /64)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ipaddress.ip_network(f"{ip}/24", strict=False).network_address) + "/24"


class RateLimiter:
    """Thread-safe in-memory sliding window rate limiter."""

    _PRUNE_EVERY = 256

    def __init__(self, max_attempts: int = 5, window_seconds: float = 60):
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._calls_since_prune = 0

    def check(self, ip: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        key = _bucket_key(ip)
        now = time.monotonic()
        with self._lock:
            attempts = [t for t in self._attempts.get(key, []) if now - t < self._window]
            if len(attempts) >= self._max_attempts:
                self._attempts[key] = attempts
                self._maybe_prune(now)
                return False
            attempts.append(now)
            self._attempts[key] = attempts
            self._maybe_prune(now)
            if len(self._attempts) > _MAX_BUCKETS:
                self._evict_oldest()
            return True

    def _maybe_prune(self, now: float) -> None:
        """Drop IP buckets whose timestamps are all outside the window."""
        self._calls_since_prune += 1
        if self._calls_since_prune < self._PRUNE_EVERY:
            return
        self._calls_since_prune = 0
        stale = [
            key for key, times in self._attempts.items()
            if not times or now - max(times) >= self._window
        ]
        for key in stale:
            self._attempts.pop(key, None)

    def _evict_oldest(self) -> None:
        """Evict the bucket whose most-recent hit is oldest."""
        if not self._attempts:
            return
        oldest_key = min(
            self._attempts,
            key=lambda k: max(self._attempts[k]) if self._attempts[k] else 0.0,
        )
        self._attempts.pop(oldest_key, None)
