"""Shared TMDB shaping, caching, and ratings-enrichment helpers.

This module hosts the cross-handler state and the helper functions used by
both ``page.py`` (the ``GET /api/search`` and ``GET /api/search/discover``
endpoints) and ``detail.py``. The handlers in those modules call into here
for normalisation, Arr-state annotation, and the parallel OMDb ratings
fan-out. All module-level singletons (rate limiters, in-memory caches,
the enrichment thread-pool) live here so the route modules stay focused
on request/response shaping.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import timedelta
from functools import lru_cache

import requests as _requests
from fastapi import Request

from mediaman.core.time import now_iso as _now_iso
from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.state import (
    ArrCaches,
    build_radarr_cache,
    build_sonarr_cache,
    compute_download_state,
)
from mediaman.services.infra.http import SafeHTTPError
from mediaman.services.media_meta.omdb import fetch_ratings, get_omdb_key
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.repository.search import fetch_ratings_cache, upsert_ratings_cache

logger = logging.getLogger(__name__)

_RATINGS_TTL_DAYS = 30
_DISCOVER_TMDB_TTL_SECONDS = 3600
_MAX_QUERY_LEN = 100
_DISCOVER_CACHE_MAX_ENTRIES = 32

_discover_cache: dict[str, tuple[float, list[dict[str, object]]]] = {}
_discover_cache_lock = threading.Lock()


@lru_cache(maxsize=1)
def _get_executor() -> ThreadPoolExecutor:
    """Return the shared ratings-enrichment executor, creating it on first call.

    Using ``lru_cache`` avoids starting 6 OS threads at import time (which
    would fire in every test that imports this module). The executor is
    created once and reused for the lifetime of the process.
    """
    return ThreadPoolExecutor(max_workers=6, thread_name_prefix="search_enrich")


# /api/search and /api/search/discover query limiter. Both endpoints fan
# out to TMDB and the per-result rating-enrichment threadpool. Even
# authenticated, an admin who scripts the endpoint (or an attacker holding
# a session cookie) can rapidly exhaust TMDB's quota or our worker pool.
# 30 per minute / 200 per day per admin keeps the typeahead UX snappy
# while blocking sustained abuse.
_QUERY_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)


_POSTER_BASE = "https://image.tmdb.org/t/p/w342"
_PROFILE_BASE = "https://image.tmdb.org/t/p/w185"
_BACKDROP_BASE = "https://image.tmdb.org/t/p/w780"


def _poster_url(path: str | None) -> str | None:
    return f"{_POSTER_BASE}{path}" if path else None


def _normalise_tmdb_item(item: dict[str, object]) -> dict[str, object] | None:
    raw_media_type = item.get("media_type")
    if raw_media_type not in ("movie", "tv"):
        return None
    media_type = str(raw_media_type)
    raw_title = item.get("title") or item.get("name") or ""
    title = str(raw_title)
    raw_date = item.get("release_date") or item.get("first_air_date") or ""
    date = str(raw_date)
    year: int | None = None
    if date[:4].isdigit():
        year = int(date[:4])
    vote = item.get("vote_average")
    poster_path = item.get("poster_path")
    return {
        "tmdb_id": item.get("id"),
        "title": title,
        "year": year,
        "poster_url": _poster_url(poster_path if isinstance(poster_path, str) else None),
        "media_type": media_type,
        "rating": round(vote, 1) if isinstance(vote, (int, float)) and vote else None,
        "popularity": item.get("popularity", 0.0),
        "download_state": None,
    }


def _annotate_states(results: list[dict[str, object]], request: Request) -> None:
    conn = get_db()
    secret_key = request.app.state.config.secret_key

    try:
        radarr_cache = build_radarr_cache(build_radarr_from_db(conn, secret_key))
    except (_requests.RequestException, SafeHTTPError, sqlite3.Error):
        logger.warning(
            "Radarr cache build failed; Search results won't reflect Radarr state", exc_info=True
        )
        radarr_cache = build_radarr_cache(None)

    try:
        sonarr_cache = build_sonarr_cache(build_sonarr_from_db(conn, secret_key))
    except (_requests.RequestException, SafeHTTPError, sqlite3.Error):
        logger.warning(
            "Sonarr cache build failed; Search results won't reflect Sonarr state", exc_info=True
        )
        sonarr_cache = build_sonarr_cache(None)

    caches: ArrCaches = {**radarr_cache, **sonarr_cache}
    for r in results:
        tmdb_id = r.get("tmdb_id")
        if tmdb_id and isinstance(tmdb_id, int):
            media_type = r["media_type"]
            r["download_state"] = compute_download_state(str(media_type), tmdb_id, caches)


# Wall-clock budget for the parallel ratings-enrichment fan-out. The previous
# code passed ``timeout=None`` to ``as_completed`` so a single stuck future
# blocked the whole iterator until ``fut.result(timeout=3)`` fired — but
# ``as_completed`` doesn't yield until the future is ready, so that inner
# timeout was effectively dead code. The right place to bound the wall-clock
# cost is on ``as_completed`` itself.
_ENRICH_BUDGET_SECONDS = 6.0


def _enrich_ratings(results: list[dict[str, object]], request: Request) -> None:
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    cutoff = (now_utc() - timedelta(days=_RATINGS_TTL_DAYS)).isoformat()

    by_key: dict[tuple[int, str], list[dict[str, object]]] = {}
    for r in results:
        tmdb_id = r.get("tmdb_id")
        if tmdb_id and isinstance(tmdb_id, int):
            media_type = r["media_type"]
            by_key.setdefault((tmdb_id, str(media_type)), []).append(r)

    if not by_key:
        return

    def _apply(group: list[dict[str, object]], rt: str | None, imdb: str | None) -> None:
        for item in group:
            if rt:
                item["rt_rating"] = rt
            if imdb:
                item["imdb_rating"] = imdb

    rows = fetch_ratings_cache(conn, list(by_key.keys()))

    misses: list[tuple[tuple[int, str], list[dict[str, object]]]] = []
    for key, group in by_key.items():
        cached = next((r for r in rows if r.tmdb_id == key[0] and r.media_type == key[1]), None)
        if cached and cached.fetched_at >= cutoff:
            _apply(group, rt=cached.rt_rating, imdb=cached.imdb_rating)
        else:
            misses.append((key, group))

    if not misses:
        return

    # Read the OMDb key in the request thread — SQLite connections must not
    # cross thread boundaries.
    resolved_omdb_key = get_omdb_key(conn, secret_key)

    def fetch(key_group):
        key, group = key_group
        probe = group[0]
        try:
            data = fetch_ratings(
                probe["title"],
                probe.get("year"),
                probe["media_type"],
                omdb_key=resolved_omdb_key,
            )
        except (SafeHTTPError, _requests.RequestException, ValueError, TypeError):
            logger.debug("Ratings fetch failed for %r — skipping", probe["title"], exc_info=True)
            data = {}
        return key, group, data

    now_iso = _now_iso()
    futures = [_get_executor().submit(fetch, kg) for kg in misses]
    pending_writes: list[tuple] = []
    try:
        for fut in as_completed(futures, timeout=_ENRICH_BUDGET_SECONDS):
            try:
                key, group, data = fut.result()
            except (SafeHTTPError, _requests.RequestException, ValueError, CancelledError):
                continue
            rt = data.get("rt")
            imdb = data.get("imdb")
            meta = data.get("metascore")
            _apply(group, rt=rt, imdb=imdb)
            pending_writes.append((key[0], key[1], imdb, rt, meta, now_iso))
    except TimeoutError:
        # Budget exhausted — anything not done is dropped silently;
        # cached results from the misses that did complete are still
        # written below so they're warm for the next call.
        logger.debug(
            "ratings enrichment timed out after %.1fs (%d/%d futures complete)",
            _ENRICH_BUDGET_SECONDS,
            sum(1 for f in futures if f.done()),
            len(futures),
        )
    if pending_writes:
        try:
            upsert_ratings_cache(conn, pending_writes)
        except sqlite3.Error:
            logger.debug("ratings_cache batch write failed", exc_info=True)
