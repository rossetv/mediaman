"""IP-based rate limiter for login attempts."""

import ipaddress
import os
import threading
import time


class RateLimiter:
    """Thread-safe in-memory sliding window rate limiter.

    ``check`` is atomic under a lock so concurrent requests cannot bypass
    the limit by racing on read-modify-write. Idle IP buckets are pruned
    opportunistically to bound memory usage against adversarial clients
    that rotate keys.
    """

    _PRUNE_EVERY = 256  # prune after this many check() calls

    def __init__(self, max_attempts: int = 5, window_seconds: float = 60):
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._calls_since_prune = 0

    def check(self, ip: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        with self._lock:
            attempts = [t for t in self._attempts.get(ip, []) if now - t < self._window]
            if len(attempts) >= self._max_attempts:
                self._attempts[ip] = attempts
                self._maybe_prune(now)
                return False
            attempts.append(now)
            self._attempts[ip] = attempts
            self._maybe_prune(now)
            return True

    def _maybe_prune(self, now: float) -> None:
        """Drop IP buckets whose timestamps are all outside the window."""
        self._calls_since_prune += 1
        if self._calls_since_prune < self._PRUNE_EVERY:
            return
        self._calls_since_prune = 0
        stale = [
            ip for ip, times in self._attempts.items()
            if not times or now - max(times) >= self._window
        ]
        for ip in stale:
            self._attempts.pop(ip, None)


def _trusted_proxies() -> list[ipaddress._BaseNetwork]:
    """Parse MEDIAMAN_TRUSTED_PROXIES env var into a list of IP networks.

    Format: comma-separated list of CIDRs or single IPs. When unset, no
    proxy is trusted and forwarded-IP headers are ignored.
    """
    raw = os.environ.get("MEDIAMAN_TRUSTED_PROXIES", "").strip()
    if not raw:
        return []
    networks: list[ipaddress._BaseNetwork] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            continue
    return networks


def _peer_is_trusted(peer: str | None, trusted: list[ipaddress._BaseNetwork]) -> bool:
    if not peer or not trusted:
        return False
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(peer_ip in net for net in trusted)


def get_client_ip(request) -> str:
    """Extract the real client IP, respecting forwarded headers only from trusted proxies.

    By default the immediate peer (``request.client.host``) is returned.
    When ``MEDIAMAN_TRUSTED_PROXIES`` is set and the peer is within a
    trusted range, the first entry from ``X-Forwarded-For`` (or, failing
    that, ``X-Real-IP``) is returned.
    """
    peer = request.client.host if request.client else None
    trusted = _trusted_proxies()
    if _peer_is_trusted(peer, trusted):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        x_real = request.headers.get("x-real-ip")
        if x_real:
            return x_real.strip()
    return peer or "unknown"
