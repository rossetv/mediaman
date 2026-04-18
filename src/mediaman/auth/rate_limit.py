"""IP-based rate limiter for login and other unauthenticated endpoints.

Thread-safe, sliding-window, in-memory. Two important hardenings over
the naive version:

1. ``get_client_ip`` walks ``X-Forwarded-For`` from the right and skips
   trusted-proxy hops, returning the first untrusted entry it sees.
   Taking the leftmost XFF entry is a classic bypass because clients
   can append arbitrary values and well-behaved proxies only *append*
   their own entry — they don't strip attacker-supplied ones.
2. ``RateLimiter`` buckets clients by network prefix (IPv6 ``/64``,
   IPv4 ``/24``) rather than exact address. Without this an attacker
   with an IPv6 ``/64`` (common home ISP allocation) can rotate
   source addresses to evade the limit trivially.

The limiter also caps total bucket count so a distributed attack
can't grow the dict without bound.
"""

import ipaddress
import os
import threading
import time


# Cap the per-IP rate-limit dict to prevent unbounded memory growth under
# a distributed attack. Oldest entries are evicted when the cap is hit.
_MAX_BUCKETS = 10_000


def _bucket_key(ip: str) -> str:
    """Collapse an IP into a network-prefix bucket key.

    IPv6 addresses fold to their ``/64`` network; IPv4 addresses fold
    to ``/24``. Unparseable inputs are passed through as-is so the
    limiter still works for ``"unknown"`` fallbacks.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ipaddress.ip_network(f"{ip}/24", strict=False).network_address) + "/24"


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
            # Hard cap on bucket count — drop oldest if exceeded.
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
        """Evict the bucket whose most-recent hit is oldest.

        Prevents the dict from growing unbounded when an attacker
        constantly introduces new prefixes.
        """
        if not self._attempts:
            return
        oldest_key = min(
            self._attempts,
            key=lambda k: max(self._attempts[k]) if self._attempts[k] else 0.0,
        )
        self._attempts.pop(oldest_key, None)


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


def _ip_in_networks(ip: str, networks: list[ipaddress._BaseNetwork]) -> bool:
    """Return True if *ip* parses and falls inside any of *networks*."""
    if not ip or not networks:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def _peer_is_trusted(peer: str | None, trusted: list[ipaddress._BaseNetwork]) -> bool:
    """Return True if the direct peer IP is in the trusted-proxy allowlist."""
    if not peer:
        return False
    return _ip_in_networks(peer, trusted)


def get_client_ip(request) -> str:
    """Extract the real client IP, respecting forwarded headers only from trusted proxies.

    When ``MEDIAMAN_TRUSTED_PROXIES`` is set and the direct peer is
    within a trusted range, the ``X-Forwarded-For`` header is walked
    from RIGHT TO LEFT, skipping entries that are themselves in the
    trusted-proxy allowlist. The first untrusted entry is returned.
    This resists the classic "rightmost wins" bypass where an attacker
    includes a spoofed leftmost entry — well-behaved proxies only
    append their own identity and do not strip client-supplied XFF
    values, so only the trailing trusted entries are reliable.

    When the peer is not trusted, the XFF header is ignored entirely.
    The ``X-Real-IP`` fallback is similarly only honoured for trusted
    peers and represents a single-value header set by upstream.
    """
    peer = request.client.host if request.client else None
    trusted = _trusted_proxies()
    if not _peer_is_trusted(peer, trusted):
        return peer or "unknown"

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        entries = [part.strip() for part in forwarded.split(",") if part.strip()]
        # Walk right-to-left, skip trusted-proxy hops, return first
        # untrusted entry (i.e. the real client as reported by the
        # nearest-to-client trusted proxy).
        for ip in reversed(entries):
            if not _ip_in_networks(ip, trusted):
                return ip
        # Every entry was a trusted proxy — fall back to the peer.
        return peer or "unknown"

    x_real = request.headers.get("x-real-ip")
    if x_real:
        return x_real.strip()

    return peer or "unknown"
