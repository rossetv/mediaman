"""Sliding-window rate limiters.

Canonical home is :mod:`mediaman.services.rate_limit`; previously lived
under ``mediaman.auth.rate_limit`` (moved R-refactor — the limiters are
domain-agnostic and not specific to auth routes).

Holds :class:`RateLimiter` (IP-bucketed) and :class:`ActionRateLimiter`
(per-actor, for authenticated admin operations).
"""

from __future__ import annotations

import collections
import ipaddress
import threading
import time

# Cap the per-IP rate-limit dict to prevent unbounded memory growth under
# a distributed attack. Oldest entries are evicted when the cap is hit.
# Tuned for a small/medium-sized self-hosted deployment: 10k unique
# /24 buckets is roughly 10k legitimate end-user subnets, well above any
# realistic concurrent load. Above this we aggressively evict the
# least-recently-used bucket. Move to a setting if larger deployments
# materialise — for now this is a safety net, not a tunable.
_MAX_BUCKETS = 10_000

# Length of the rolling window used by ActionRateLimiter's daily cap.
# 24 hours, expressed in seconds.
_ONE_DAY_SECONDS = 86_400.0


class ActionRateLimiter:
    """Per-actor rate limiter for authenticated admin operations.

    Keyed on username. Thread-safe; enforces both a short burst window
    and a rolling 24h cap.

    Sliding 24h window
    ------------------
    The daily cap is enforced as a *sliding* 24h window (timestamps of
    every successful check within the last 24h are retained, and
    anything older is dropped). The previous calendar-day implementation
    let an attacker double their daily budget across the UTC midnight
    boundary by sending the day-N quota at 23:59:59 then the day-N+1
    quota at 00:00:01.

    Trade-off: memory is now ``O(max_per_day)`` per actor instead of a
    single integer. With ``max_per_day`` typically in the tens to low
    hundreds, this is a modest fixed cost (a few hundred bytes per
    actor). Acceptable for the security gain.
    """

    def __init__(self, max_in_window: int, window_seconds: float, max_per_day: int = 0):
        self._max_in_window = max_in_window
        self._window = window_seconds
        self._max_per_day = max_per_day
        self._attempts: dict[str, list[float]] = {}
        # Sliding 24h timestamp log per actor. Only populated when
        # ``max_per_day > 0``; we use a deque so old entries can be
        # popped from the left in O(1).
        self._daily: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()
        self._calls_since_prune = 0

    _DAILY_PRUNE_EVERY = 512

    def check(self, actor: str) -> bool:
        """Return True if the action is allowed, False if rate-limited."""
        now = time.monotonic()
        with self._lock:
            if self._max_per_day > 0:
                day_log = self._daily.get(actor)
                if day_log is not None:
                    cutoff = now - _ONE_DAY_SECONDS
                    while day_log and day_log[0] < cutoff:
                        day_log.popleft()
                    if not day_log:
                        # Drop empty deque so the actor doesn't linger.
                        self._daily.pop(actor, None)
                        day_log = None
                count = len(day_log) if day_log is not None else 0
                if count >= self._max_per_day:
                    return False

            attempts = [t for t in self._attempts.get(actor, []) if now - t < self._window]
            if len(attempts) >= self._max_in_window:
                self._attempts[actor] = attempts
                return False

            if self._max_per_day > 0:
                day_log = self._daily.get(actor)
                if day_log is None:
                    day_log = collections.deque()
                    self._daily[actor] = day_log
                day_log.append(now)
                self._calls_since_prune += 1
                if self._calls_since_prune >= self._DAILY_PRUNE_EVERY:
                    self._calls_since_prune = 0
                    self._prune_daily(now)

            attempts.append(now)
            self._attempts[actor] = attempts
            return True

    def _prune_daily(self, now: float) -> None:
        """Drop actors whose entire 24h log has aged out."""
        cutoff = now - _ONE_DAY_SECONDS
        stale: list[str] = []
        for actor, log in self._daily.items():
            while log and log[0] < cutoff:
                log.popleft()
            if not log:
                stale.append(actor)
        for actor in stale:
            self._daily.pop(actor, None)

    def reset(self) -> None:
        """Test-helper for clearing all bucket state.

        Drops every actor's burst-window history and rolling 24h log so
        suites that share a module-level limiter can guarantee a clean
        slate between cases. Not for production use.
        """
        with self._lock:
            self._attempts.clear()
            self._daily.clear()
            self._calls_since_prune = 0


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
    """Thread-safe in-memory sliding window rate limiter.

    Eviction is O(1) per call: buckets are stored in an
    :class:`collections.OrderedDict` ordered by most-recent access, and
    the least-recently-used bucket is popped when the cap is hit.
    """

    _PRUNE_EVERY = 256

    def __init__(self, max_attempts: int = 5, window_seconds: float = 60):
        self._max_attempts = max_attempts
        self._window = window_seconds
        # OrderedDict so we can move buckets to the end on access and
        # popitem(last=False) to evict the least-recently-used in O(1).
        self._attempts: collections.OrderedDict[str, list[float]] = collections.OrderedDict()
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
                self._attempts.move_to_end(key)
                self._maybe_prune(now)
                return False
            attempts.append(now)
            self._attempts[key] = attempts
            self._attempts.move_to_end(key)
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
            key
            for key, times in self._attempts.items()
            if not times or now - max(times) >= self._window
        ]
        for key in stale:
            self._attempts.pop(key, None)

    def _evict_oldest(self) -> None:
        """Evict the least-recently-used bucket in O(1)."""
        if not self._attempts:
            return
        # popitem(last=False) removes the first (LRU) entry.
        self._attempts.popitem(last=False)

    def reset(self) -> None:
        """Test-helper for clearing all bucket state.

        Drops every IP bucket's timestamp history so suites that share a
        module-level limiter can guarantee a clean slate between cases.
        Not for production use.
        """
        with self._lock:
            self._attempts.clear()
            self._calls_since_prune = 0
