"""Shared TMDB shaping, caching and ratings-enrichment helpers for page.py and detail.py. All
module-level singletons (rate limiters, in-memory caches, the enrichment thread pool) live
here so the route modules stay focused on request/response shaping.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import (
    CancelledError,
    Future,
    ThreadPoolExecutor,
    as_completed,
)
from datetime import timedelta
from functools import lru_cache

import requests as _requests
from fastapi import Request

from mediaman.core.time import now_iso as _now_iso
from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.arr.state import (
    annotate_download_states,
)
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.omdb import get_omdb_key, lookup_ratings
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.watch_catalog import TmdbUnavailable, tmdb_image_url
from mediaman.web.repository.search import (
    RatingsCacheRow,
    fetch_ratings_cache,
    upsert_ratings_cache,
)

logger = logging.getLogger(__name__)

_RATINGS_TTL_DAYS = 30
_DISCOVER_FRESH_S = 6 * 3600
_DISCOVER_STALE_S = 7 * 24 * 3600
_MAX_QUERY_LEN = 100
# A worker thread waits for the answer.
_TMDB_TIMEOUT_S = 3.0


def _fetch_discover_shelf(
    shelf_key: str,
    fetch_fn: Callable[[int], list[dict[str, object]]],
    inject_media_type: str | None,
    page: int,
    allow_fetch: bool = True,
) -> list[dict[str, object]]:
    """Fetch one page of a discover shelf through the disk cache (fresh 6 h, served stale up
    to 7 d), stamping media_type on each item when inject_media_type is set (the popular
    endpoints omit it). The client answers [] on a failure, so an empty page is not cached
    and the shelf comes back empty. The key matches Watch's discover rows for page 1, so both
    raise the same TmdbUnavailable: the cache hands a stored failure to whoever asks next.
    Without allow_fetch only a cached shelf is answered.
    """

    def fetch() -> list[dict[str, object]]:
        raw = fetch_fn(page)
        if not raw:
            raise TmdbUnavailable(shelf_key)
        if inject_media_type:
            return [{**item, "media_type": inject_media_type} for item in raw]
        return raw

    try:
        shelf: list[dict[str, object]] = disk_cache.meta().get_or_fetch(
            f"tmdb:discover:{shelf_key}:{page}",
            _DISCOVER_FRESH_S,
            _DISCOVER_STALE_S,
            fetch,
            allow_fetch=lambda: allow_fetch,
        )
    except (TmdbUnavailable, disk_cache.Refused):
        return []
    return shelf


@lru_cache(maxsize=1)
def _get_executor() -> ThreadPoolExecutor:
    """Return the shared ratings-enrichment executor, created on first call; lru_cache avoids
    starting 6 OS threads at import time in every test that imports this module.
    """
    return ThreadPoolExecutor(max_workers=6, thread_name_prefix="search_enrich")


# /api/search and /api/search/discover query limiter. Both endpoints fan
# out to TMDB and the per-result rating-enrichment threadpool. Even
# authenticated, an admin who scripts the endpoint (or an attacker holding
# a session cookie) can rapidly exhaust TMDB's quota or our worker pool.
# 30 per minute / 200 per day per admin keeps the typeahead UX snappy
# while blocking sustained abuse.
_QUERY_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)


def _poster_url(path: str | None) -> str | None:
    return tmdb_image_url(path, "w342")


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
    annotate_download_states(results, conn, secret_key)


# Wall-clock budget for the parallel ratings-enrichment fan-out. It is
# passed to ``as_completed`` because that call does not yield until a future
# is ready, so a timeout on ``fut.result`` alone would never fire and one
# stuck future would block the whole iterator.
_ENRICH_BUDGET_SECONDS = 6.0

# After OMDb gives no answer (an outage, a spent quota) nothing is cached, so
# every search would ask again for each miss and wait out the budget. Pause the
# asking for this long and show what is stored instead.
_NO_ANSWER_PAUSE_SECONDS = 60.0
_paused_until = 0.0
_OUTAGE_MIN_MISSES = 3

# Type alias for the ``(key, group)`` pairs threaded through the ratings
# fan-out: a ``(tmdb_id, media_type)`` key and the result dicts sharing it.
_RatingsKeyGroup = tuple[tuple[int, str], list[dict[str, object]]]
# What a single ratings future resolves to: the key, its group, and the
# ratings dict fetched from OMDb (None when OMDb gave no answer).
_RatingsFetchResult = tuple[tuple[int, str], list[dict[str, object]], dict[str, str] | None]


def _apply_ratings(group: list[dict[str, object]], rt: str | None, imdb: str | None) -> None:
    for item in group:
        if rt:
            item["rt_rating"] = rt
        if imdb:
            item["imdb_rating"] = imdb


def _partition_cached(
    by_key: dict[tuple[int, str], list[dict[str, object]]],
    rows: list[RatingsCacheRow],
    cutoff: str,
) -> list[_RatingsKeyGroup]:
    misses: list[_RatingsKeyGroup] = []
    for key, group in by_key.items():
        cached = next(
            (row for row in rows if row.tmdb_id == key[0] and row.media_type == key[1]), None
        )
        if cached:
            # A stale row still shows if OMDb cannot be asked or does not answer.
            _apply_ratings(group, rt=cached.rt_rating, imdb=cached.imdb_rating)
        if not cached or cached.fetched_at < cutoff:
            misses.append((key, group))
    return misses


def _fetch_ratings_for_group(
    key_group: _RatingsKeyGroup, omdb_key: str | None
) -> _RatingsFetchResult:
    """Fetch OMDb ratings for one miss group on the enrichment pool. Returns (key, group,
    data); data is None when OMDb gave no answer, so a single bad lookup cannot abort the
    fan-out and is not cached as "no ratings".
    """
    key, group = key_group
    probe = group[0]
    try:
        year_raw = probe.get("year")
        year_int: int | None = year_raw if isinstance(year_raw, int) else None
        data: dict[str, str] | None = lookup_ratings(
            str(probe["title"]),
            year_int,
            str(probe["media_type"]),
            omdb_key=omdb_key,
        )
    except (SafeHTTPError, _requests.RequestException, ValueError, TypeError):
        logger.debug("Ratings fetch failed for %r — skipping", probe["title"], exc_info=True)
        data = None
    return key, group, data


def _drain_futures(
    futures: list[Future[_RatingsFetchResult]], now_iso: str
) -> list[tuple[int, str, str | None, str | None, str | None, str]]:
    """Collect completed ratings futures within the wall-clock budget and return the rows to
    batch-write to the cache. On TimeoutError unfinished futures are dropped silently, but
    results from the misses that did complete are still returned so they are warm next time.
    """
    global _paused_until
    pending_writes: list[tuple[int, str, str | None, str | None, str | None, str]] = []
    answered = unanswered = False
    try:
        for fut in as_completed(futures, timeout=_ENRICH_BUDGET_SECONDS):
            try:
                key, group, data = fut.result()
            except (SafeHTTPError, _requests.RequestException, ValueError, CancelledError):
                unanswered = True
                continue
            if data is None:
                unanswered = True
                continue
            answered = True
            rt = data.get("rt")
            imdb = data.get("imdb")
            meta = data.get("metascore")
            _apply_ratings(group, rt=rt, imdb=imdb)
            pending_writes.append((key[0], key[1], imdb, rt, meta, now_iso))
    except TimeoutError:
        unanswered = True
        logger.debug(
            "ratings enrichment timed out after %.1fs (%d/%d futures complete)",
            _ENRICH_BUDGET_SECONDS,
            sum(1 for f in futures if f.done()),
            len(futures),
        )
    # One bad title, a slow but answering OMDb, or a round of fewer than three misses is not
    # an outage: pause only when three or more misses got no answer at all.
    if unanswered and not answered and len(futures) >= _OUTAGE_MIN_MISSES:
        _paused_until = time.monotonic() + _NO_ANSWER_PAUSE_SECONDS
    return pending_writes


def _stamp_omdb_ratings(results: list[dict[str, object]], request: Request) -> None:
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    cutoff = (now_utc() - timedelta(days=_RATINGS_TTL_DAYS)).isoformat()

    by_key: dict[tuple[int, str], list[dict[str, object]]] = {}
    for search_result in results:
        tmdb_id = search_result.get("tmdb_id")
        if tmdb_id and isinstance(tmdb_id, int):
            media_type = search_result["media_type"]
            by_key.setdefault((tmdb_id, str(media_type)), []).append(search_result)

    if not by_key:
        return

    rows = fetch_ratings_cache(conn, list(by_key.keys()))
    misses = _partition_cached(by_key, rows, cutoff)
    if not misses:
        return

    # Read the OMDb key in the request thread — SQLite connections must not
    # cross thread boundaries.
    resolved_omdb_key = get_omdb_key(conn, secret_key)
    if not resolved_omdb_key or time.monotonic() < _paused_until:
        return

    now_iso = _now_iso()
    futures = [
        _get_executor().submit(_fetch_ratings_for_group, kg, resolved_omdb_key) for kg in misses
    ]
    pending_writes = _drain_futures(futures, now_iso)
    if pending_writes:
        try:
            upsert_ratings_cache(conn, pending_writes)
        except sqlite3.Error:
            # A persistent write fault means every search permanently re-fans
            # out to OMDb with nothing cached — surface it loudly, not at DEBUG.
            logger.warning("ratings_cache batch write failed", exc_info=True)
