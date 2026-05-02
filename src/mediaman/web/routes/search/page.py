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

from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.db import get_db
from mediaman.services.media_meta.tmdb import TmdbClient

from ._enrichment import (
    _DISCOVER_TMDB_TTL_SECONDS,
    _MAX_QUERY_LEN,
    _QUERY_LIMITER,
    _annotate_states,
    _discover_cache,
    _discover_cache_lock,
    _enrich_ratings,
    _normalise_tmdb_item,
)

logger = logging.getLogger("mediaman")

router = APIRouter()


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request) -> Response:
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
    if not _QUERY_LIMITER.check(admin):
        logger.warning("search.query_throttled user=%s", admin)
        return JSONResponse(
            {"error": "Too many search requests — slow down"},
            status_code=429,
        )
    if len(q) < 2:
        return JSONResponse({"results": []})
    q = q[:_MAX_QUERY_LEN]
    conn = get_db()
    client = TmdbClient.from_db(conn, request.app.state.config.secret_key)
    if client is None:
        return JSONResponse({"error": "TMDB not configured"}, status_code=502)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(client.search_multi_paged, q, 1)
        f2 = pool.submit(client.search_multi_paged, q, 2)
        page1 = f1.result()
        page2 = f2.result()

    if not page1 and not page2:
        return JSONResponse({"error": "TMDB request failed"}, status_code=502)

    raw = page1 + page2
    shaped = [s for s in (_normalise_tmdb_item(x) for x in raw) if s is not None][:40]
    _annotate_states(shaped, request)
    _enrich_ratings(shaped, request)
    return JSONResponse({"results": shaped})


@router.get("/api/search/discover")
def api_discover(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    if not _QUERY_LIMITER.check(admin):
        logger.warning("search.discover_throttled user=%s", admin)
        return JSONResponse(
            {"error": "Too many discover requests — slow down"},
            status_code=429,
        )
    conn = get_db()
    client = TmdbClient.from_db(conn, request.app.state.config.secret_key)
    if client is None:
        return JSONResponse({"error": "TMDB not configured"}, status_code=502)

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
