"""Search routes."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Literal, TypedDict

import requests as _requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.auth.rate_limit import ActionRateLimiter, RateLimiter, get_client_ip
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.state import (
    RadarrCaches,
    SonarrCaches,
    build_radarr_cache,
    build_sonarr_cache,
    compute_download_state,
)
from mediaman.services.downloads.notifications import record_download_notification as _record_dn
from mediaman.services.infra.http_client import SafeHTTPError
from mediaman.services.infra.time import now_iso as _now_iso
from mediaman.services.media_meta.omdb import fetch_ratings, get_omdb_key
from mediaman.services.media_meta.tmdb import TmdbClient

_RATINGS_TTL_DAYS = 30
_DISCOVER_TMDB_TTL_SECONDS = 3600


@lru_cache(maxsize=1)
def _get_executor() -> ThreadPoolExecutor:
    """Return the shared ratings-enrichment executor, creating it on first call.

    Using ``lru_cache`` avoids starting 6 OS threads at import time (which
    would fire in every test that imports this module). The executor is
    created once and reused for the lifetime of the process.
    """
    return ThreadPoolExecutor(max_workers=6, thread_name_prefix="search_enrich")


_MAX_QUERY_LEN = 100

_discover_cache: dict[str, tuple[float, list[dict[str, object]]]] = {}
_discover_cache_lock = threading.Lock()

logger = logging.getLogger("mediaman")

# ---------------------------------------------------------------------------
# Rate limiters for POST /api/search/download (finding 33)
# ---------------------------------------------------------------------------

# Per-admin burst limiter: 20 downloads per minute, 200 per day.
_DOWNLOAD_ADMIN_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=200,
)

# Per-IP limiter: 30 downloads per minute (covers unauthenticated / shared sessions).
_DOWNLOAD_IP_LIMITER = RateLimiter(max_attempts=30, window_seconds=60)

# /api/search and /api/search/discover query limiter (findings 1, 2). Both
# endpoints fan out to TMDB and the per-result rating-enrichment threadpool.
# Even authenticated, an admin who scripts the endpoint (or an attacker
# holding a session cookie) can rapidly exhaust TMDB's quota or our worker
# pool. 30 per minute / 200 per day per admin keeps the typeahead UX
# snappy while blocking sustained abuse.
_QUERY_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=200)

# Duplicate-request suppression: block identical (username, tmdb_id, media_type, seasons)
# submissions within a short window to prevent accidental double-clicks from adding
# duplicates. The season set is included in the dedup key so a TV submission for
# seasons {1,2} does not block a follow-up submission for season {3} on the same
# series (finding 5).
_DOWNLOAD_DEDUP_WINDOW_SECONDS = 10.0
_download_dedup: dict[tuple[str, int, str, str], float] = {}
_download_dedup_lock = threading.Lock()


def _seasons_dedup_token(
    monitored_seasons: list[int] | None,
    search_seasons: list[int] | None,
) -> str:
    """Return a stable deterministic token for the season selection.

    Treats ``None`` (all seasons) and an empty list distinctly so the
    "monitor everything" submission can't collide with a "monitor only
    season 1" request. Sorting both lists makes the token order-stable
    regardless of how the client serialised them.
    """
    mon = "*" if monitored_seasons is None else ",".join(str(s) for s in sorted(monitored_seasons))
    srch = "*" if search_seasons is None else ",".join(str(s) for s in sorted(search_seasons))
    return f"m={mon};s={srch}"


def _is_duplicate_download(
    username: str,
    tmdb_id: int,
    media_type: str,
    seasons_token: str = "",
) -> bool:
    """Return True if an identical request was submitted within the dedup window.

    Cleans up stale entries on each call to bound memory growth.
    """
    key = (username, tmdb_id, media_type, seasons_token)
    now = time.monotonic()
    with _download_dedup_lock:
        # Prune stale entries.
        stale = [
            k for k, ts in _download_dedup.items() if now - ts > _DOWNLOAD_DEDUP_WINDOW_SECONDS
        ]
        for k in stale:
            _download_dedup.pop(k, None)
        if key in _download_dedup:
            return True
        _download_dedup[key] = now
        return False


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


_POSTER_BASE = "https://image.tmdb.org/t/p/w342"
_PROFILE_BASE = "https://image.tmdb.org/t/p/w185"
_BACKDROP_BASE = "https://image.tmdb.org/t/p/w780"


def _poster_url(path: str | None) -> str | None:
    return f"{_POSTER_BASE}{path}" if path else None


def _normalise_tmdb_item(item: dict) -> dict | None:
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

    caches = {**radarr_cache, **sonarr_cache}
    for r in results:
        if r.get("tmdb_id"):
            r["download_state"] = compute_download_state(r["media_type"], r["tmdb_id"], caches)


# Wall-clock budget for the parallel ratings-enrichment fan-out (finding 4).
# The previous code passed ``timeout=None`` to ``as_completed`` so a single
# stuck future blocked the whole iterator until ``fut.result(timeout=3)``
# fired — but ``as_completed`` doesn't yield until the future is ready, so
# that inner timeout was effectively dead code. The right place to bound
# the wall-clock cost is on ``as_completed`` itself.
_ENRICH_BUDGET_SECONDS = 6.0


def _enrich_ratings(results: list[dict], request: Request) -> None:
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    cutoff = (datetime.now(UTC) - timedelta(days=_RATINGS_TTL_DAYS)).isoformat()

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
    for tmdb_id, media_type in by_key:
        flat.extend([tmdb_id, media_type])
    rows = conn.execute(
        f"SELECT tmdb_id, media_type, imdb_rating, rt_rating, metascore, fetched_at "
        f"FROM ratings_cache WHERE (tmdb_id, media_type) IN ({placeholders})",
        flat,
    ).fetchall()

    misses: list[tuple[tuple[int, str], list[dict]]] = []
    for key, group in by_key.items():
        cached = next(
            (r for r in rows if r["tmdb_id"] == key[0] and r["media_type"] == key[1]), None
        )
        if cached and cached["fetched_at"] >= cutoff:
            _apply(group, rt=cached["rt_rating"], imdb=cached["imdb_rating"])
        else:
            misses.append((key, group))

    if not misses:
        return

    # Read the OMDb key in the request thread — SQLite connections must not
    # cross thread boundaries (finding 32).
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
        except Exception:
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
            except Exception:
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
            conn.executemany(
                "INSERT OR REPLACE INTO ratings_cache "
                "(tmdb_id, media_type, imdb_rating, rt_rating, metascore, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                pending_writes,
            )
            conn.commit()
        except sqlite3.Error:
            logger.debug("ratings_cache batch write failed", exc_info=True)


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


def _pick_trailer(videos: list[dict]) -> str | None:
    fallback: str | None = None
    for v in videos:
        if v.get("site") != "YouTube":
            continue
        key = v.get("key")
        if not key:
            continue
        if v.get("type") == "Trailer":
            return key
        if fallback is None:
            fallback = key
    return fallback


class _SonarrDetail(TypedDict):
    tracked: bool
    seasons_in_library: set[int]


def _fetch_sonarr_series_detail(tmdb_id: int, sonarr_cache: dict, client) -> _SonarrDetail:
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


def _extract_credits(data: dict, media_type: str) -> tuple[int | None, str | None, list[dict]]:
    if media_type == "movie":
        runtime = data.get("runtime")
        director: str | None = next(
            (
                c.get("name")
                for c in (data.get("credits") or {}).get("crew", [])
                if c.get("job") == "Director"
            ),
            None,
        )
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
    return runtime, director, cast


def _build_arr_caches(
    conn: sqlite3.Connection,
    secret_key: str,
    media_type: str,
) -> tuple[RadarrCaches, SonarrCaches, object]:
    if media_type == "movie":
        try:
            radarr_cache = build_radarr_cache(build_radarr_from_db(conn, secret_key))
        except (_requests.RequestException, SafeHTTPError, sqlite3.Error):
            logger.warning("Radarr cache build failed during detail fetch", exc_info=True)
            radarr_cache = build_radarr_cache(None)
        return radarr_cache, build_sonarr_cache(None), None

    radarr_cache = build_radarr_cache(None)
    try:
        sonarr_client = build_sonarr_from_db(conn, secret_key)
        sonarr_cache = build_sonarr_cache(sonarr_client)
    except (_requests.RequestException, SafeHTTPError, sqlite3.Error):
        logger.warning("Sonarr cache build failed during detail fetch", exc_info=True)
        sonarr_client = None
        sonarr_cache = build_sonarr_cache(None)
    return radarr_cache, sonarr_cache, sonarr_client


@router.get("/api/search/detail/{media_type}/{tmdb_id}")
def api_detail(
    media_type: str,
    tmdb_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")

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

    runtime, director, cast = _extract_credits(data, media_type)
    trailer_key = _pick_trailer((data.get("videos") or {}).get("results") or [])
    ratings = fetch_ratings(title, year, media_type, conn=conn, secret_key=secret_key)

    radarr_cache, sonarr_cache, sonarr_client = _build_arr_caches(conn, secret_key, media_type)
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
        "backdrop_url": f"{_BACKDROP_BASE}{data['backdrop_path']}"
        if data.get("backdrop_path")
        else None,
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
                "year": int(s["air_date"][:4])
                if s.get("air_date") and s["air_date"][:4].isdigit()
                else None,
                "in_library": s["season_number"] in seasons_in_lib,
            }
            for s in data.get("seasons", [])
            if s.get("season_number", 0) > 0
        ]

    return JSONResponse(out)


class _DownloadRequest(BaseModel):
    """Body schema for ``POST /api/search/download`` (finding 3).

    ``extra="forbid"`` means an unknown key from the client raises HTTP 422
    rather than being silently ignored. ``media_type`` is constrained to
    the two values the route actually handles, the title is bounded to
    256 chars, and the season lists are capped at 100 entries — generous
    for any real series but tight enough to refuse a flood payload.
    """

    model_config = ConfigDict(extra="forbid")

    media_type: Literal["movie", "tv"]
    tmdb_id: int = Field(ge=1)
    title: str = Field(max_length=256)
    monitored_seasons: list[int] | None = Field(default=None, max_length=100)
    search_seasons: list[int] | None = Field(default=None, max_length=100)


# ``admin_users`` has no ``email`` column — the username is the only
# identifier for the admin account, so it is intentionally re-used as
# the notification ``email`` field on download_notifications rows. The
# notification subsystem treats this as an opaque identifier (it does
# not actually attempt SMTP delivery to a non-email value), but the
# coupling is documented here so a future schema migration to a real
# email column does not quietly leave this call site stale (finding 7).
def _resolve_admin_email(admin: str) -> str:
    """Return the notification identifier for *admin*.

    Currently this is just the admin username; see the module-level
    note above for why this is intentional.
    """
    return admin


@router.post("/api/search/download")
def api_download(
    body: _DownloadRequest, request: Request, admin: str = Depends(get_current_admin)
) -> JSONResponse:
    # Per-admin rate check.
    if not _DOWNLOAD_ADMIN_LIMITER.check(admin):
        logger.warning("search.download_throttled user=%s", admin)
        return JSONResponse(
            {"ok": False, "error": "Too many download requests — slow down"},
            status_code=429,
        )

    # Per-IP rate check.
    client_ip = get_client_ip(request)
    if not _DOWNLOAD_IP_LIMITER.check(client_ip):
        logger.warning("search.download_ip_throttled ip=%s", client_ip)
        return JSONResponse(
            {"ok": False, "error": "Too many download requests from this IP — slow down"},
            status_code=429,
        )

    # Duplicate-request suppression: same (admin, tmdb_id, media_type, seasons)
    # within the dedup window indicates a double-click or replay (finding 5).
    seasons_token = _seasons_dedup_token(body.monitored_seasons, body.search_seasons)
    if _is_duplicate_download(admin, body.tmdb_id, body.media_type, seasons_token):
        logger.info(
            "search.download_duplicate_suppressed user=%s tmdb=%s type=%s seasons=%s",
            admin,
            body.tmdb_id,
            body.media_type,
            seasons_token,
        )
        return JSONResponse(
            {"ok": False, "error": "Duplicate request — wait a moment before retrying"},
            status_code=429,
        )

    conn = get_db()
    secret_key = request.app.state.config.secret_key

    # Notification recipient: see ``_resolve_admin_email`` for the
    # rationale on re-using the admin username (finding 7).
    notify_email = _resolve_admin_email(admin)

    if body.media_type == "movie":
        radarr = build_radarr_from_db(conn, secret_key)
        if not radarr:
            return JSONResponse({"ok": False, "error": "Radarr not configured"}, status_code=503)
        try:
            if radarr.get_movie_by_tmdb(body.tmdb_id):
                return JSONResponse(
                    {"ok": False, "error": f"'{body.title}' is already in your library"},
                    status_code=409,
                )
            radarr.add_movie(body.tmdb_id, body.title)
        except (SafeHTTPError, _requests.RequestException, ValueError):
            # Narrow exception list (finding 6): SafeHTTPError covers
            # Radarr's non-2xx responses, RequestException covers
            # transport failures, ValueError covers add_movie's own
            # ``tmdb_id <= 0`` guard. A bare ``except Exception`` would
            # silently swallow programming bugs.
            logger.exception("Failed to add movie")
            return JSONResponse({"ok": False, "error": "Failed to add to Radarr"}, status_code=502)
        _record_dn(
            conn,
            email=notify_email,
            title=body.title,
            media_type="movie",
            tmdb_id=body.tmdb_id,
            service="radarr",
        )
        conn.commit()
        return JSONResponse({"ok": True, "message": f"Added '{body.title}' to Radarr"})

    sonarr = build_sonarr_from_db(conn, secret_key)
    if not sonarr:
        return JSONResponse({"ok": False, "error": "Sonarr not configured"}, status_code=503)
    try:
        lookup = sonarr.lookup_series_by_tmdb(body.tmdb_id)
    except (SafeHTTPError, _requests.RequestException):
        logger.exception("Sonarr lookup failed")
        return JSONResponse({"ok": False, "error": "Sonarr lookup failed"}, status_code=502)
    if not lookup:
        return JSONResponse(
            {"ok": False, "error": "Series not found in Sonarr lookup"}, status_code=404
        )
    tvdb_id = lookup.get("tvdbId")
    if not tvdb_id:
        return JSONResponse({"ok": False, "error": "No TVDB ID for this series"}, status_code=422)

    try:
        existing = sonarr.get_series()
    except (SafeHTTPError, _requests.RequestException):
        logger.warning(
            "Sonarr get_series failed during duplicate check — skipping check", exc_info=True
        )
        existing = []
    if any(s.get("tvdbId") == tvdb_id for s in existing):
        return JSONResponse(
            {
                "ok": False,
                "error": f"'{body.title}' is already tracked by Sonarr — manage it from the Library page or Sonarr directly",
            },
            status_code=409,
        )

    if body.search_seasons is not None and len(body.search_seasons) == 0:
        return JSONResponse({"ok": False, "error": "Pick at least one season"}, status_code=400)

    try:
        if body.monitored_seasons is None:
            sonarr.add_series(tvdb_id, body.title)
        else:
            sonarr.add_series_with_seasons(
                tvdb_id,
                body.title,
                body.monitored_seasons,
                body.search_seasons or [],
            )
    except (SafeHTTPError, _requests.RequestException, ValueError):
        logger.exception("Failed to add series")
        return JSONResponse({"ok": False, "error": "Failed to add to Sonarr"}, status_code=502)
    _record_dn(
        conn,
        email=notify_email,
        title=body.title,
        media_type="tv",
        tmdb_id=body.tmdb_id,
        tvdb_id=tvdb_id,
        service="sonarr",
    )
    conn.commit()
    return JSONResponse({"ok": True, "message": f"Added '{body.title}' to Sonarr"})
