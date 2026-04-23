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
from __future__ import annotations

import ipaddress
import os
import threading
import time

# Cap the per-IP rate-limit dict to prevent unbounded memory growth under
# a distributed attack. Oldest entries are evicted when the cap is hit.
_MAX_BUCKETS = 10_000


class ActionRateLimiter:
    """Per-actor rate limiter for authenticated admin operations.

    Distinct from the IP-bucket limiter because the goal is different:
    this caps what a compromised-credential attacker can do with a
    valid session before the real admin notices and kicks them out.
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

    # Prune _day_counts after this many check() calls to bound memory.
    # Keyed by actor username so a large admin set is the bound — 10 k
    # is generous for any realistic deployment.
    _DAY_COUNT_PRUNE_EVERY = 512

    def check(self, actor: str) -> bool:
        """Return True if the action is allowed, False if rate-limited.

        Ordering: check the daily cap first (read-only) so an actor that
        has exhausted their day allowance doesn't also consume a slot in
        the burst window. The burst window is checked second and only
        bumped when the action is actually permitted.
        """
        now = time.monotonic()
        today = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            # --- Daily cap (check before bumping the burst window) ----------
            if self._max_per_day > 0:
                day_key, day_count = self._day_counts.get(actor, ("", 0))
                if day_key != today:
                    # Day rolled over — reset the count for this actor.
                    day_count = 0
                if day_count >= self._max_per_day:
                    return False

            # --- Burst window -----------------------------------------------
            attempts = [t for t in self._attempts.get(actor, []) if now - t < self._window]
            if len(attempts) >= self._max_in_window:
                self._attempts[actor] = attempts
                return False

            # --- Permitted: bump both counters ------------------------------
            if self._max_per_day > 0:
                day_key, day_count = self._day_counts.get(actor, ("", 0))
                if day_key != today:
                    day_count = 0
                self._day_counts[actor] = (today, day_count + 1)
                # Prune stale day_counts entries periodically to bound memory.
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


def trusted_proxies() -> list[ipaddress._BaseNetwork]:
    """Return the list of trusted proxy networks from MEDIAMAN_TRUSTED_PROXIES.

    Format: comma-separated CIDRs or single IPs.
    When unset, no proxy is trusted and forwarded-IP headers are ignored.
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


def peer_is_trusted(peer: str | None, trusted: list[ipaddress._BaseNetwork]) -> bool:
    """Return True if the direct peer IP is in the trusted-proxy allowlist."""
    if not peer:
        return False
    return _ip_in_networks(peer, trusted)


def get_client_ip(request) -> str:
    """Extract the real client IP, respecting forwarded headers only from trusted proxies.

    Resolution order:

    1. If the direct peer is a trusted proxy AND it sent
       ``CF-Connecting-IP`` (Cloudflare) — use that. Cloudflare strips
       client-supplied ``CF-Connecting-IP`` at the edge, so this header
       is attacker-unforgeable when CF fronts the origin.
    2. If the direct peer is a trusted proxy, walk ``X-Forwarded-For``
       from RIGHT TO LEFT, skipping entries that are themselves trusted
       proxies, and return the first untrusted entry. This resists the
       "leftmost wins" bypass.
    3. If the direct peer is trusted but no XFF is present, honour
       ``X-Real-IP`` (single-value header).
    4. Otherwise (no trusted proxy configured, or peer is not in the
       list) — return the direct peer. Do NOT trust forwarded headers,
       because an attacker can put anything in them.

    The right way to configure this on a real deployment: set
    ``MEDIAMAN_TRUSTED_PROXIES`` to the CIDR of the reverse-proxy
    network (e.g. Cloudflare's published IPs, or a specific internal
    proxy) and ensure nothing else is reachable. If unset, we fail
    closed: every request buckets on the CF-edge IP instead of the
    real client, which costs availability (more false rate-limit hits)
    but gains security (no spoof bypass).
    """
    peer = request.client.host if request.client else None
    trusted = trusted_proxies()
    if not peer_is_trusted(peer, trusted):
        return peer or "unknown"

    # Cloudflare's CF-Connecting-IP is the real client IP and CF
    # strips any client-supplied value at the edge — always prefer
    # it when present.
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        cf_ip = cf_ip.strip()
        # Minimal sanity check — a bad value falls through to XFF.
        try:
            ipaddress.ip_address(cf_ip)
            return cf_ip
        except ValueError:
            pass  # Invalid CF-Connecting-IP header — fall through to X-Forwarded-For

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
        x_real = x_real.strip()
        # Validate before trusting — a misconfigured or lying proxy could
        # deliver a non-IP value and let an attacker spoof their apparent
        # address. On parse failure, fall through to the direct peer.
        try:
            ipaddress.ip_address(x_real)
            return x_real
        except ValueError:
            pass  # Malformed X-Real-IP — fall through to direct peer

    return peer or "unknown"
