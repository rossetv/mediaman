"""Search page and TMDB-backed list endpoints (GET /search, GET /api/search, GET
/api/search/discover). The shared fetch, annotation and ratings pipeline lives in
._enrichment.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.db import get_db
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_user, resolve_page_session
from mediaman.web.responses import respond_err

from ._enrichment import (
    _MAX_QUERY_LEN,
    _QUERY_LIMITER,
    _annotate_states,
    _fetch_discover_shelf,
    _normalise_tmdb_item,
    _stamp_omdb_ratings,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _normalise_shelf(raw: Iterable[dict[str, object]], limit: int) -> list[dict[str, object]]:
    """Normalise raw TMDB items and cap the result at limit. The slice applies after filtering
    out unusable items, so the cap counts usable results, not raw API entries.
    """
    normalised = (_normalise_tmdb_item(tmdb_item) for tmdb_item in raw)
    return [item for item in normalised if item is not None][:limit]


@router.get("/search")
def search_page(request: Request, q: str = "") -> Response:
    """Send the old search page to the Watch page, keeping a usable query."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    q = q.strip()[:_MAX_QUERY_LEN]
    return RedirectResponse(f"/watch?{urlencode({'q': q})}" if q else "/watch", status_code=302)


@router.get("/api/search")
def api_search(q: str, request: Request, admin: str = Depends(get_current_user)) -> JSONResponse:
    """Search TMDB for movies and TV shows matching q: pages 1 and 2 in parallel, up to 40
    results, with download state and OMDb ratings.
    """
    if not _QUERY_LIMITER.check(admin):
        logger.warning("search.query_throttled user=%s", _sanitise_log_field(admin))
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
    shaped = _normalise_shelf(raw, 40)
    _annotate_states(shaped, request)
    _stamp_omdb_ratings(shaped, request)
    return JSONResponse({"results": shaped})


@router.get("/api/search/discover")
def api_discover(request: Request, admin: str = Depends(get_current_user)) -> JSONResponse:
    """Return trending and popular shelves from TMDB, each capped at 21 items. Shelves are
    fetched in parallel and TTL-cached per shelf key to avoid redundant TMDB calls.
    """
    if not _QUERY_LIMITER.check(admin):
        logger.warning("search.discover_throttled user=%s", _sanitise_log_field(admin))
        return respond_err(
            "too_many_requests", status=429, message="Too many discover requests — slow down"
        )
    conn = get_db()
    client = TmdbClient.from_db(conn, request.app.state.config.secret_key)
    if client is None:
        return respond_err("tmdb_not_configured", status=502)

    with ThreadPoolExecutor(max_workers=6) as pool:
        f_trending_1 = pool.submit(_fetch_discover_shelf, "trending", client.trending, None, 1)
        f_trending_2 = pool.submit(_fetch_discover_shelf, "trending", client.trending, None, 2)
        f_movies_1 = pool.submit(
            _fetch_discover_shelf, "popular_movies", client.popular_movies, "movie", 1
        )
        f_movies_2 = pool.submit(
            _fetch_discover_shelf, "popular_movies", client.popular_movies, "movie", 2
        )
        f_tv_1 = pool.submit(_fetch_discover_shelf, "popular_tv", client.popular_tv, "tv", 1)
        f_tv_2 = pool.submit(_fetch_discover_shelf, "popular_tv", client.popular_tv, "tv", 2)
        trending_raw = f_trending_1.result() + f_trending_2.result()
        movies_raw = f_movies_1.result() + f_movies_2.result()
        tv_raw = f_tv_1.result() + f_tv_2.result()

    trending = _normalise_shelf(trending_raw, 21)
    popular_movies = _normalise_shelf(movies_raw, 21)
    popular_tv = _normalise_shelf(tv_raw, 21)

    combined = trending + popular_movies + popular_tv
    _annotate_states(combined, request)
    _stamp_omdb_ratings(combined, request)

    return JSONResponse(
        {
            "trending": trending,
            "popular_movies": popular_movies,
            "popular_tv": popular_tv,
        }
    )
