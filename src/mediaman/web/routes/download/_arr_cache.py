"""Process-wide TTL cache for Radarr/Sonarr state used by the download flow.

Each GET /download/{token} previously issued four outbound HTTP calls
(Radarr movies + queue, Sonarr series + queue) on every render. With
one valid public token, an attacker driving the rate limit (30 req/min)
would multiply that into 120 outbound requests/min/IP — effectively a
request amplifier against the operator's home Arr boxes. Cache the
per-service snapshot for a short window so a burst of confirm-page
loads collapses to one set of upstream calls.

TTL is 30 s: long enough to absorb a confirm-page burst, short enough
that a state change (admin adds a movie elsewhere) is reflected on the
next click without manual refresh. The cache is process-local so
multi-worker deploys re-fetch per worker — that is acceptable since
the rate limit is per-IP per-worker as well.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time

from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.state import (
    RadarrCaches,
    SonarrCaches,
    build_radarr_cache,
    build_sonarr_cache,
)

_ARR_CACHE_TTL_SECONDS = 30.0
# rationale: module-level mutable cache is required so concurrent confirm-page
# renders in the same worker collapse to one outbound Radarr/Sonarr round-trip
# per service per TTL window. The threading.Lock below guards every read and
# write; see CODE_GUIDELINES §8.5.
_ARR_CACHE_LOCK = threading.Lock()
# (service_name, secret_key_fingerprint) -> (timestamp, cache_payload).
# The payload is either a RadarrCaches or SonarrCaches TypedDict; the
# caller knows which one to expect from the service tag in the key.
_ARR_CACHE: dict[tuple[str, str], tuple[float, RadarrCaches | SonarrCaches]] = {}


def _key_fingerprint(secret_key: str) -> str:
    """Short fingerprint of *secret_key* for use as a cache key.

    The full key never appears in the cache; only its first 16 hex chars
    of a SHA-256 digest. Different deployments with different secrets do
    not collide.
    """
    return hashlib.sha256(secret_key.encode()).hexdigest()[:16]


def _get_radarr_cache_cached(conn: sqlite3.Connection, secret_key: str) -> RadarrCaches:
    """Return the Radarr cache dict, using a process-wide TTL cache."""
    key = ("radarr", _key_fingerprint(secret_key))
    now = time.monotonic()
    with _ARR_CACHE_LOCK:
        hit = _ARR_CACHE.get(key)
        if hit and now - hit[0] < _ARR_CACHE_TTL_SECONDS:
            return hit[1]  # type: ignore[return-value]
    radarr_client = build_radarr_from_db(conn, secret_key)
    cache = build_radarr_cache(radarr_client)
    with _ARR_CACHE_LOCK:
        _ARR_CACHE[key] = (now, cache)
    return cache


def _get_sonarr_cache_cached(conn: sqlite3.Connection, secret_key: str) -> SonarrCaches:
    """Return the Sonarr cache dict, using a process-wide TTL cache."""
    key = ("sonarr", _key_fingerprint(secret_key))
    now = time.monotonic()
    with _ARR_CACHE_LOCK:
        hit = _ARR_CACHE.get(key)
        if hit and now - hit[0] < _ARR_CACHE_TTL_SECONDS:
            return hit[1]  # type: ignore[return-value]
    sonarr_client = build_sonarr_from_db(conn, secret_key)
    cache = build_sonarr_cache(sonarr_client)
    with _ARR_CACHE_LOCK:
        _ARR_CACHE[key] = (now, cache)
    return cache


def _reset_arr_cache_for_tests() -> None:
    """Clear the Arr-state cache. Test helper; never call in production."""
    with _ARR_CACHE_LOCK:
        _ARR_CACHE.clear()
