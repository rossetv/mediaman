"""Search page — TMDB-backed discovery and download."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response
from pydantic import BaseModel

from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.db import get_db
from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.tmdb import TmdbClient
from mediaman.services.download_notifications import record_download_notification as _record_dn
from mediaman.services.omdb import fetch_ratings

_RATINGS_TTL_DAYS = 30
_DISCOVER_TMDB_TTL_SECONDS = 3600

# In-process cache of raw TMDB discover responses keyed by request path.
# Trending/popular shelves change slowly (TMDB recomputes daily), so an hour-long
# TTL skips three external roundtrips on every /search page load without making
# arr/ratings state stale — those are re-applied per request downstream.
_discover_cache: dict[str, tuple[float, list[dict]]] = {}
_discover_cache_lock = threading.Lock()

logger = logging.getLogger("mediaman")

router = APIRouter()

_POSTER_BASE = "https://image.tmdb.org/t/p/w342"
_PROFILE_BASE = "https://image.tmdb.org/t/p/w185"
_BACKDROP_BASE = "https://image.tmdb.org/t/p/w780"


def _poster_url(path: str | None) -> str | None:
    """Return the full TMDB poster URL for a given path, or None."""
    return f"{_POSTER_BASE}{path}" if path else None


def _shape_result(item: dict) -> dict | None:
    """Reduce a TMDB multi/trending item to the search-card shape.

    Filters out ``person`` entries — only ``movie`` and ``tv`` are kept.
    """
    media_type = item.get("media_type")
    if media_type not in ("movie", "tv"):
        return None
    title = item.get("title") or item.get("name") or ""
    date = item.get("release_date") or item.get("first_air_date") or ""
    year: int | None = None
    if date[:4].isdigit():
        year = int(date[:4])
    vote = item.get("vote_average")
    return {
        "tmdb_id": item.get("id"),
        "title": title,
        "year": year,
        "poster_url": _poster_url(item.get("poster_path")),
        "media_type": media_type,
        "rating": round(vote, 1) if isinstance(vote, (int, float)) and vote else None,
        "popularity": item.get("popularity", 0.0),
        "download_state": None,
    }



def _annotate_states(results: list[dict], request: Request) -> None:
    """Fill in ``download_state`` for each result in place.

    Arr failures degrade gracefully: if Radarr or Sonarr are configured
    but unreachable, the affected half of the cache stays empty and
    items from that media type simply report no download_state.
    """
    from mediaman.services.arr_state import (
        build_radarr_cache,
        build_sonarr_cache,
        compute_download_state,
    )
    conn = get_db()
    secret_key = request.app.state.config.secret_key

    try:
        radarr_cache = build_radarr_cache(build_radarr_from_db(conn, secret_key))
    except Exception:
        logger.warning("Radarr cache build failed; Search results won't reflect Radarr state", exc_info=True)
        radarr_cache = build_radarr_cache(None)

    try:
        sonarr_cache = build_sonarr_cache(build_sonarr_from_db(conn, secret_key))
    except Exception:
        logger.warning("Sonarr cache build failed; Search results won't reflect Sonarr state", exc_info=True)
        sonarr_cache = build_sonarr_cache(None)

    caches = {**radarr_cache, **sonarr_cache}
    for r in results:
        if r.get("tmdb_id"):
            r["download_state"] = compute_download_state(r["media_type"], r["tmdb_id"], caches)


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "search.html", {
        "username": username,
        "nav_active": "search",
    })


def _enrich_ratings(results: list[dict], request: Request) -> None:
    """Fill ``rt_rating`` (and ``imdb_rating``) on each result via a SQLite-backed
    cache of OMDb lookups. Cache misses are fetched concurrently with a tight
    timeout so search-as-you-type stays responsive; OMDb failures degrade silently.

    Groups results by (tmdb_id, media_type) — the discover endpoint can ship the
    same title in multiple shelves (e.g. Trending and Popular Movies), and every
    duplicate dict must receive the enriched ratings, not just whichever one
    happened to be last in the input list.
    """
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_RATINGS_TTL_DAYS)).isoformat()

    by_key: dict[tuple[int, str], list[dict]] = {}
    for r in results:
        tmdb_id = r.get("tmdb_id")
        if tmdb_id:
            by_key.setdefault((tmdb_id, r["media_type"]), []).append(r)

    if not by_key:
        return

    def _apply(group: list[dict], rt: str | None, imdb: str | None) -> None:
        for item in group:
            if rt:
                item["rt_rating"] = rt
            if imdb:
                item["imdb_rating"] = imdb

    placeholders = ",".join(["(?, ?)"] * len(by_key))
    flat: list = []
    for (tmdb_id, media_type) in by_key.keys():
        flat.extend([tmdb_id, media_type])
    rows = conn.execute(
        f"SELECT tmdb_id, media_type, imdb_rating, rt_rating, metascore, fetched_at "
        f"FROM ratings_cache WHERE (tmdb_id, media_type) IN ({placeholders})",
        flat,
    ).fetchall()

    misses: list[tuple[tuple[int, str], list[dict]]] = []
    for key, group in by_key.items():
        cached = next((r for r in rows if r["tmdb_id"] == key[0] and r["media_type"] == key[1]), None)
        if cached and cached["fetched_at"] >= cutoff:
            _apply(group, rt=cached["rt_rating"], imdb=cached["imdb_rating"])
        else:
            misses.append((key, group))

    if not misses:
        return

    def fetch(key_group):
        key, group = key_group
        probe = group[0]
        try:
            data = fetch_ratings(probe["title"], probe.get("year"), probe["media_type"], conn=conn, secret_key=secret_key)
        except Exception:
            data = {}
        return key, group, data

    now_iso = datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch, kg) for kg in misses]
        for fut in as_completed(futures, timeout=None):
            try:
                key, group, data = fut.result(timeout=3)
            except Exception:
                continue
            rt = data.get("rt")
            imdb = data.get("imdb")
            meta = data.get("metascore")
            _apply(group, rt=rt, imdb=imdb)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO ratings_cache "
                    "(tmdb_id, media_type, imdb_rating, rt_rating, metascore, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key[0], key[1], imdb, rt, meta, now_iso),
                )
            except Exception:
                logger.debug("ratings_cache write failed", exc_info=True)
        conn.commit()


@router.get("/api/search")
def api_search(q: str, request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return up to ~40 TMDB multi-search hits (two pages, concurrently).

    TMDB's ``/search/multi`` returns 20 hits per page. Fetching page 1 and 2 in
    parallel doubles coverage for ambiguous queries without doubling latency.
    If either page fails in isolation the other still contributes, and only a
    double-failure surfaces a 502.
    """
    if len(q) < 2:
        return JSONResponse({"results": []})
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
    shaped = [s for s in (_shape_result(x) for x in raw) if s is not None][:40]
    _annotate_states(shaped, request)
    _enrich_ratings(shaped, request)
    return JSONResponse({"results": shaped})


@router.get("/api/search/discover")
def api_discover(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return the empty-state browse shelves in a single response.

    Six TMDB calls fire concurrently (pages 1+2 of trending / popular movies /
    popular TV) so each shelf can surface 21 cards despite TMDB's 20-per-page
    cap. Radarr+Sonarr caches build once and are applied to every shelf, and
    ratings enrichment dedupes across shelves so a title appearing in multiple
    lists only pays one OMDb roundtrip. Any individual page that fails comes
    back empty — the endpoint only 502s if TMDB isn't configured at all.
    """
    conn = get_db()
    client = TmdbClient.from_db(conn, request.app.state.config.secret_key)
    if client is None:
        return JSONResponse({"error": "TMDB not configured"}, status_code=502)

    def _fetch_cached(
        shelf_key: str,
        fetch_fn,
        inject_media_type: str | None,
        page: int,
    ) -> list[dict]:
        """Call ``fetch_fn(page)`` with a per-shelf TTL cache.

        Results from ``/movie/popular`` and ``/tv/popular`` don't include a
        ``media_type`` field; ``inject_media_type`` patches that in place so
        ``_shape_result`` can filter them correctly.
        """
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

    # TMDB returns 20 results per page, so fetch pages 1 and 2 per shelf to
    # reach the 21-card display target. The 6 calls still fan out in parallel
    # and each page caches independently.
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

    trending = [s for s in (_shape_result(x) for x in trending_raw) if s is not None][:21]
    popular_movies = [s for s in (_shape_result(x) for x in movies_raw) if s is not None][:21]
    popular_tv = [s for s in (_shape_result(x) for x in tv_raw) if s is not None][:21]

    # Single pass over the union so Arr cache build and OMDb enrichment both
    # run once regardless of cross-shelf duplicates.
    combined = trending + popular_movies + popular_tv
    _annotate_states(combined, request)
    _enrich_ratings(combined, request)

    return JSONResponse({
        "trending": trending,
        "popular_movies": popular_movies,
        "popular_tv": popular_tv,
    })


def _pick_trailer(videos: list[dict]) -> str | None:
    """Return the YouTube key for the first Trailer, then any YouTube video."""
    for v in videos:
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            return v.get("key")
    for v in videos:
        if v.get("site") == "YouTube":
            return v.get("key")
    return None


def _fetch_sonarr_series_detail(tmdb_id: int, sonarr_cache: dict, client) -> dict:
    """Return ``{'tracked': bool, 'seasons_in_library': set[int]}`` for a TV show.

    Reuses the ``sonarr_cache`` already built for download_state and the
    ``client`` already constructed upstream — avoids a second client
    construction and round-trip to Sonarr for the same series list.
    """
    if not client:
        return {"tracked": False, "seasons_in_library": set()}
    lookup = client.lookup_series_by_tmdb(tmdb_id)
    if not lookup:
        return {"tracked": False, "seasons_in_library": set()}
    tvdb_id = lookup.get("tvdbId")
    if not tvdb_id:
        return {"tracked": False, "seasons_in_library": set()}
    all_series = list(sonarr_cache.get("sonarr_series", {}).values())
    added = next((s for s in all_series if s.get("tvdbId") == tvdb_id), None)
    if not added:
        return {"tracked": False, "seasons_in_library": set()}
    in_library = {
        s.get("seasonNumber")
        for s in added.get("seasons", [])
        if (s.get("statistics") or {}).get("episodeFileCount", 0) > 0
    }
    return {"tracked": True, "seasons_in_library": in_library}


@router.get("/api/search/detail/{media_type}/{tmdb_id}")
def api_detail(
    media_type: str,
    tmdb_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Return rich detail for a single movie or TV show from TMDB.

    Appends videos and credits in a single TMDB call, then enriches the
    response with IMDb/RT/Metascore ratings, download state, and — for TV —
    per-season library status from Sonarr.
    """
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")

    from mediaman.services.tmdb import TmdbClient

    conn = get_db()
    secret_key = request.app.state.config.secret_key
    client = TmdbClient.from_db(conn, secret_key)
    if client is None:
        return JSONResponse({"error": "TMDB not configured"}, status_code=502)

    data = client.details(media_type, tmdb_id)
    if data is None:
        return JSONResponse({"error": "TMDB request failed"}, status_code=502)

    title = data.get("title") or data.get("name") or ""
    date = data.get("release_date") or data.get("first_air_date") or ""
    year = int(date[:4]) if date[:4].isdigit() else None

    if media_type == "movie":
        runtime = data.get("runtime")
        credits = data.get("credits") or {}
        director = None
        for crew in credits.get("crew", []):
            if crew.get("job") == "Director":
                director = crew.get("name")
                break
    else:
        ert = data.get("episode_run_time") or []
        runtime = ert[0] if ert else None
        creators = data.get("created_by") or []
        director = creators[0].get("name") if creators else None

    cast_raw = (data.get("credits") or {}).get("cast") or []
    cast = [
        {
            "name": c.get("name"),
            "character": c.get("character"),
            "profile_url": f"{_PROFILE_BASE}{c['profile_path']}" if c.get("profile_path") else None,
        }
        for c in cast_raw[:6]
    ]

    trailer_key = _pick_trailer((data.get("videos") or {}).get("results") or [])
    ratings = fetch_ratings(title, year, media_type, conn=conn, secret_key=secret_key)

    from mediaman.services.arr_state import (
        build_radarr_cache,
        build_sonarr_cache,
        compute_download_state,
    )

    sonarr_client = None
    if media_type == "movie":
        try:
            radarr_cache = build_radarr_cache(build_radarr_from_db(conn, secret_key))
        except Exception:
            logger.warning("Radarr cache build failed during detail fetch", exc_info=True)
            radarr_cache = build_radarr_cache(None)
        sonarr_cache = build_sonarr_cache(None)
    else:
        radarr_cache = build_radarr_cache(None)
        try:
            sonarr_client = build_sonarr_from_db(conn, secret_key)
            sonarr_cache = build_sonarr_cache(sonarr_client)
        except Exception:
            logger.warning("Sonarr cache build failed during detail fetch", exc_info=True)
            sonarr_cache = build_sonarr_cache(None)

    caches = {**radarr_cache, **sonarr_cache}
    state = compute_download_state(media_type, tmdb_id, caches)

    out: dict = {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "year": year,
        "tagline": data.get("tagline") or None,
        "description": data.get("overview") or "",
        "poster_url": _poster_url(data.get("poster_path")),
        "backdrop_url": f"{_BACKDROP_BASE}{data['backdrop_path']}" if data.get("backdrop_path") else None,
        "runtime": runtime,
        "genres": [g["name"] for g in data.get("genres", [])],
        "director": director,
        "cast": cast,
        "trailer_key": trailer_key,
        "rating_tmdb": round(data["vote_average"], 1) if data.get("vote_average") else None,
        "download_state": state,
    }
    if "imdb" in ratings:
        out["rating_imdb"] = ratings["imdb"]
    if "rt" in ratings:
        out["rating_rt"] = ratings["rt"]
    if "metascore" in ratings:
        out["rating_metascore"] = ratings["metascore"]

    if media_type == "tv":
        sonarr_info = _fetch_sonarr_series_detail(tmdb_id, sonarr_cache, sonarr_client)
        out["sonarr_tracked"] = sonarr_info["tracked"]
        seasons_in_lib = sonarr_info["seasons_in_library"]
        out["seasons"] = [
            {
                "season_number": s["season_number"],
                "name": s.get("name") or f"Season {s['season_number']}",
                "episode_count": s.get("episode_count", 0),
                "year": int(s["air_date"][:4]) if s.get("air_date") and s["air_date"][:4].isdigit() else None,
                "in_library": s["season_number"] in seasons_in_lib,
            }
            for s in data.get("seasons", [])
            if s.get("season_number", 0) > 0
        ]

    return JSONResponse(out)


class _DownloadRequest(BaseModel):
    media_type: str
    tmdb_id: int
    title: str
    monitored_seasons: list[int] | None = None
    search_seasons: list[int] | None = None



@router.post("/api/search/download")
def api_download(body: _DownloadRequest, request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Add a movie or TV series to Radarr / Sonarr from the Search page.

    For TV, if ``monitored_seasons`` is omitted the full series is added via
    ``add_series``; otherwise ``add_series_with_seasons`` is used for selective
    season tracking. An empty ``search_seasons`` list is rejected — the caller
    must pick at least one season to search.
    """
    if body.media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")
    conn = get_db()
    secret_key = request.app.state.config.secret_key

    admin_row = conn.execute(
        "SELECT email FROM subscribers WHERE active=1 LIMIT 1"
    ).fetchone()
    notify_email = admin_row["email"] if admin_row else admin

    if body.media_type == "movie":
        radarr = build_radarr_from_db(conn, secret_key)
        if not radarr:
            return JSONResponse({"ok": False, "error": "Radarr not configured"})
        try:
            if radarr.get_movie_by_tmdb(body.tmdb_id):
                return JSONResponse({"ok": False, "error": f"'{body.title}' is already in your library"})
            radarr.add_movie(body.tmdb_id, body.title)
        except Exception:
            logger.exception("Failed to add movie")
            return JSONResponse({"ok": False, "error": "Failed to add to Radarr"})
        _record_dn(conn, email=notify_email, title=body.title, media_type="movie", tmdb_id=body.tmdb_id, service="radarr")
        conn.commit()
        return JSONResponse({"ok": True, "message": f"Added '{body.title}' to Radarr"})

    # TV
    sonarr = build_sonarr_from_db(conn, secret_key)
    if not sonarr:
        return JSONResponse({"ok": False, "error": "Sonarr not configured"})
    try:
        lookup = sonarr.lookup_series_by_tmdb(body.tmdb_id)
    except Exception:
        logger.exception("Sonarr lookup failed")
        return JSONResponse({"ok": False, "error": "Sonarr lookup failed"})
    if not lookup:
        return JSONResponse({"ok": False, "error": "Series not found in Sonarr lookup"})
    tvdb_id = lookup.get("tvdbId")
    if not tvdb_id:
        return JSONResponse({"ok": False, "error": "No TVDB ID for this series"})

    try:
        existing = sonarr.get_series()
    except Exception:
        existing = []
    if any(s.get("tvdbId") == tvdb_id for s in existing):
        return JSONResponse({
            "ok": False,
            "error": f"'{body.title}' is already tracked by Sonarr — manage it from the Library page or Sonarr directly",
        })

    if body.search_seasons is not None and len(body.search_seasons) == 0:
        return JSONResponse({"ok": False, "error": "Pick at least one season"})

    try:
        if body.monitored_seasons is None:
            sonarr.add_series(tvdb_id, body.title)
        else:
            sonarr.add_series_with_seasons(
                tvdb_id, body.title, body.monitored_seasons, body.search_seasons or [],
            )
    except Exception:
        logger.exception("Failed to add series")
        return JSONResponse({"ok": False, "error": "Failed to add to Sonarr"})
    _record_dn(conn, email=notify_email, title=body.title, media_type="tv", tmdb_id=body.tmdb_id, tvdb_id=tvdb_id, service="sonarr")
    conn.commit()
    return JSONResponse({"ok": True, "message": f"Added '{body.title}' to Sonarr"})
