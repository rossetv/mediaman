"""Search page and TMDB-backed list endpoints.

Hosts:

* ``GET /search`` — the search HTML page.
* ``GET /api/search`` — typeahead/multi-search endpoint.
* ``GET /api/search/discover`` — Trending/Popular shelves endpoint.

The list endpoints share a TMDB-paged-fetch + state-annotation + ratings
fan-out pipeline; those helpers live in :mod:`._enrichment` so this module
focuses on request/response shaping.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.db import get_db
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.web.auth.middleware import get_current_admin, resolve_page_session
from mediaman.web.responses import respond_err

from ._enrichment import (
    _DISCOVER_CACHE_MAX_ENTRIES,
    _DISCOVER_TMDB_TTL_SECONDS,
    _MAX_QUERY_LEN,
    _QUERY_LIMITER,
    _annotate_states,
    _discover_cache,
    _discover_cache_lock,
    _enrich_ratings,
    _normalise_tmdb_item,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request) -> Response:
    """Render the media search page. Redirects to /login if the session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, _conn = resolved
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "username": username,
            "nav_active": "search",
        },
    )


@router.get("/api/search")
def api_search(q: str, request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Search TMDB for movies and TV shows matching *q*.

    Fetches pages 1 and 2 in parallel, normalises each result, annotates
    download state, and enriches with OMDb ratings. Returns up to 40 results.

    Args:
        q: Search query (must be at least 2 characters).
        request: Incoming FastAPI request (used to resolve app state).
        admin: Authenticated admin username (rate-limit key).

    Returns:
        JSON with a ``results`` list, or an error response on rate-limit or
        TMDB misconfiguration.
    """
    if not _QUERY_LIMITER.check(admin):
        logger.warning("search.query_throttled user=%s", admin)
        return respond_err(
            "too_many_requests", status=429, message="Too many search requests — slow down"
        )
    if len(q) < 2:
        return JSONResponse({"results": []})
    q = q[:_MAX_QUERY_LEN]
    conn = get_db()
    client = TmdbClient.from_db(conn, request.app.state.config.secret_key)
    if client is None:
        return respond_err("tmdb_not_configured", status=502)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(client.search_multi_paged, q, 1)
        f2 = pool.submit(client.search_multi_paged, q, 2)
        page1 = f1.result()
        page2 = f2.result()

    if not page1 and not page2:
        return respond_err("tmdb_request_failed", status=502)

    raw = page1 + page2
    shaped = [s for s in (_normalise_tmdb_item(x) for x in raw) if s is not None][:40]
    _annotate_states(shaped, request)
    _enrich_ratings(shaped, request)
    return JSONResponse({"results": shaped})


@router.get("/api/search/discover")
def api_discover(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return trending and popular shelves from TMDB for the discover view.

    Fetches trending, popular movies, and popular TV pages in parallel across
    a thread pool; results are TTL-cached per shelf key to avoid redundant TMDB
    calls. Each shelf is normalised, state-annotated, and ratings-enriched before
    being returned.

    Returns:
        JSON with ``trending``, ``popular_movies``, and ``popular_tv`` lists,
        each capped at 21 items.
    """
    if not _QUERY_LIMITER.check(admin):
        logger.warning("search.discover_throttled user=%s", admin)
        return respond_err(
            "too_many_requests", status=429, message="Too many discover requests — slow down"
        )
    conn = get_db()
    client = TmdbClient.from_db(conn, request.app.state.config.secret_key)
    if client is None:
        return respond_err("tmdb_not_configured", status=502)

    def _fetch_cached(shelf_key, fetch_fn, inject_media_type, page):
        cache_key = f"{shelf_key}?page={page}"
        now = time.monotonic()
        with _discover_cache_lock:
            entry = _discover_cache.get(cache_key)
            if entry and now - entry[0] < _DISCOVER_TMDB_TTL_SECONDS:
                return entry[1]
        raw = fetch_fn(page)
        if inject_media_type:
            for x in raw:
                x["media_type"] = inject_media_type
        with _discover_cache_lock:
            # rationale: bounded to prevent unbounded growth on malformed inputs;
            # small clear-on-overflow is fine because TTL means stale entries
            # refresh on next read.
            if len(_discover_cache) >= _DISCOVER_CACHE_MAX_ENTRIES:
                _discover_cache.clear()
            _discover_cache[cache_key] = (now, raw)
        return raw

    with ThreadPoolExecutor(max_workers=6) as pool:
        f_trending_1 = pool.submit(_fetch_cached, "trending", client.trending, None, 1)
        f_trending_2 = pool.submit(_fetch_cached, "trending", client.trending, None, 2)
        f_movies_1 = pool.submit(_fetch_cached, "popular_movies", client.popular_movies, "movie", 1)
        f_movies_2 = pool.submit(_fetch_cached, "popular_movies", client.popular_movies, "movie", 2)
        f_tv_1 = pool.submit(_fetch_cached, "popular_tv", client.popular_tv, "tv", 1)
        f_tv_2 = pool.submit(_fetch_cached, "popular_tv", client.popular_tv, "tv", 2)
        trending_raw = f_trending_1.result() + f_trending_2.result()
        movies_raw = f_movies_1.result() + f_movies_2.result()
        tv_raw = f_tv_1.result() + f_tv_2.result()

    trending = [s for s in (_normalise_tmdb_item(x) for x in trending_raw) if s is not None][:21]
    popular_movies = [s for s in (_normalise_tmdb_item(x) for x in movies_raw) if s is not None][
        :21
    ]
    popular_tv = [s for s in (_normalise_tmdb_item(x) for x in tv_raw) if s is not None][:21]

    combined = trending + popular_movies + popular_tv
    _annotate_states(combined, request)
    _enrich_ratings(combined, request)

    return JSONResponse(
        {
            "trending": trending,
            "popular_movies": popular_movies,
            "popular_tv": popular_tv,
        }
    )
