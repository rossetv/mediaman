"""Download routes package — token-authenticated download/re-download confirmations."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import TypedDict
from urllib.parse import quote as _url_quote

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from mediaman.auth.audit import log_audit
from mediaman.auth.middleware import get_optional_admin
from mediaman.auth.rate_limit import RateLimiter, get_client_ip
from mediaman.crypto import generate_poll_token, validate_download_token, validate_poll_token
from mediaman.db import get_db
from mediaman.services.arr_build import (
    build_radarr_from_db,
    build_sonarr_from_db,
)
from mediaman.services.arr_state import (
    build_radarr_cache,
    build_sonarr_cache,
    compute_download_state,
)
from mediaman.services.download_format import (
    build_episode_summary,
    build_item,
    extract_poster_url,
    fmt_episode_label,
    map_arr_status,
)
from mediaman.services.download_notifications import record_download_notification
from mediaman.services.download_queue import build_episode_dicts
from mediaman.services.format import format_bytes
from mediaman.services.http_client import SafeHTTPError
from mediaman.services.item_enrichment import enrich_redownload_item

# YouTube video IDs are exactly 11 URL-safe base64 characters.
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

logger = logging.getLogger("mediaman")

router = APIRouter()

# Rate limiter for the public download endpoint.
_DOWNLOAD_LIMITER_GET = RateLimiter(max_attempts=30, window_seconds=60)
_DOWNLOAD_LIMITER_POST = RateLimiter(max_attempts=10, window_seconds=60)

# Status polling is called frequently by the UI.
_DOWNLOAD_STATUS_LIMITER = RateLimiter(max_attempts=120, window_seconds=60)

# In-memory single-use cache for download-token POSTs.
_USED_TOKENS_LOCK = threading.Lock()
_USED_TOKENS: dict[str, float] = {}


def _mark_token_used(token: str, exp: int) -> bool:
    """Atomically mark *token* as consumed. Return False if already used."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _USED_TOKENS_LOCK:
        if len(_USED_TOKENS) > 1000:
            for k, v in list(_USED_TOKENS.items()):
                if v < now:
                    _USED_TOKENS.pop(k, None)
        if digest in _USED_TOKENS:
            return False
        _USED_TOKENS[digest] = float(exp)
        return True


def _unmark_token_used(token: str) -> None:
    """Release a previously claimed token so the user can retry."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _USED_TOKENS_LOCK:
        _USED_TOKENS.pop(digest, None)


def _validate_youtube_id(s: str | None) -> str | None:
    """Return *s* if it is a valid YouTube video ID, else None."""
    if not s:
        return None
    return s if _YOUTUBE_ID_RE.match(s) else None


class _DownloadItem(TypedDict, total=False):
    """Shape of the item dict threaded through the download page handlers."""
    title: str
    media_type: str
    tmdb_id: int | None
    email: str
    action: str
    poster_url: str | None
    year: int | None
    description: str | None
    reason: str | None
    rating: float | None
    rt_rating: str | None
    tagline: str | None
    runtime: int | None
    genres: str | None
    cast_json: str | None
    director: str | None
    trailer_key: str | None
    imdb_rating: str | None
    metascore: str | None
    genres_list: list
    cast_list: list


def _base_download_item(payload: dict) -> _DownloadItem:
    """Build the skeleton download item from a validated token payload."""
    return {
        "title":       payload.get("title", ""),
        "media_type":  payload.get("mt", ""),
        "tmdb_id":     payload.get("tmdb"),
        "email":       payload.get("email", ""),
        "action":      payload.get("act", "download"),
        "poster_url":  None,
        "year":        None,
        "description": None,
        "reason":      None,
        "rating":      None,
        "rt_rating":   None,
        "tagline":     None,
        "runtime":     None,
        "genres":      None,
        "cast_json":   None,
        "director":    None,
        "trailer_key": None,
        "imdb_rating": None,
        "metascore":   None,
        "genres_list": [],
        "cast_list":   [],
    }


def _build_item_from_suggestion(payload: dict, row) -> dict:
    """Build a fully-populated download item from a suggestions DB row."""
    item = _base_download_item(payload)
    item.update({
        "poster_url":  row["poster_url"],
        "year":        row["year"],
        "description": row["description"],
        "reason":      row["reason"],
        "rating":      row["rating"],
        "rt_rating":   row["rt_rating"],
        "tagline":     row["tagline"],
        "runtime":     row["runtime"],
        "genres":      row["genres"],
        "cast_json":   row["cast_json"],
        "director":    row["director"],
        "trailer_key": _validate_youtube_id(row["trailer_key"]),
        "imdb_rating": row["imdb_rating"],
        "metascore":   row["metascore"],
    })
    return item


def _format_timeleft(timeleft: str) -> str:
    """Convert HH:MM:SS timeleft string to a human-readable eta string."""
    if not timeleft:
        return ""
    parts = timeleft.split(":")
    if len(parts) != 3:
        return ""
    try:
        hours, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return ""
    if hours > 0:
        return f"~{hours} hr {mins:02d} min remaining"
    if mins > 0:
        return f"~{mins} min remaining"
    return f"~{max(1, secs)} sec remaining"


@router.get("/download/{token}", response_class=HTMLResponse)
def download_page(request: Request, token: str) -> HTMLResponse:
    """Render the download confirmation page."""
    config = request.app.state.config
    templates = request.app.state.templates
    conn = get_db()

    if not _DOWNLOAD_LIMITER_GET.check(get_client_ip(request)):
        return HTMLResponse("Too many requests. Try again later.", status_code=429)

    if len(token) > 4096:
        return templates.TemplateResponse(request, "download.html", {
            "state": "expired",
            "item": None,
        })

    payload = validate_download_token(token, config.secret_key)
    if payload is None:
        return templates.TemplateResponse(request, "download.html", {
            "state": "expired",
            "item": None,
        })

    sid = payload.get("sid")
    if sid:
        row = conn.execute(
            "SELECT poster_url, year, description, reason, rating, rt_rating, "
            "tagline, runtime, genres, cast_json, director, trailer_key, imdb_rating, metascore "
            "FROM suggestions WHERE id = ?",
            (sid,),
        ).fetchone()
        item = _build_item_from_suggestion(payload, row) if row else _base_download_item(payload)
    elif payload.get("act") == "redownload":
        item = _base_download_item(payload)
        enrich_redownload_item(item, conn, config.secret_key)
    else:
        item = _base_download_item(payload)

    item["trailer_key"] = _validate_youtube_id(item.get("trailer_key"))

    if item.get("genres"):
        try:
            item["genres_list"] = json.loads(item["genres"])
        except (json.JSONDecodeError, TypeError):
            item["genres_list"] = []
    if item.get("cast_json"):
        try:
            item["cast_list"] = json.loads(item["cast_json"])
        except (json.JSONDecodeError, TypeError):
            item["cast_list"] = []

    item["download_state"] = None
    tmdb_id = payload.get("tmdb")
    if tmdb_id:
        try:
            radarr_client = build_radarr_from_db(conn, config.secret_key)
            sonarr_client = build_sonarr_from_db(conn, config.secret_key)
            radarr_cache = build_radarr_cache(radarr_client)
            sonarr_cache = build_sonarr_cache(sonarr_client)
            caches = {**radarr_cache, **sonarr_cache}
            mt = "movie" if payload.get("mt") == "movie" else "tv"
            state = compute_download_state(mt, tmdb_id, caches)
            if state is not None:
                item["download_state"] = state
        except (requests.RequestException, SafeHTTPError):
            logger.warning("Failed to check Arr library status for tmdb_id=%s", tmdb_id, exc_info=True)

    hero_item = None
    if item["download_state"] == "queued":
        service = "radarr" if item["media_type"] == "movie" else "sonarr"
        hero_item = build_item(
            dl_id=f"{service}:{item['title']}",
            title=item["title"],
            media_type=item["media_type"],
            poster_url=item.get("poster_url") or "",
            state="searching",
            progress=0,
            eta="",
            size_done="",
            size_total="",
        )

    return templates.TemplateResponse(request, "download.html", {
        "state": "confirm",
        "item":  item,
        "token": token,
        "hero_item": hero_item,
    })


@router.post("/download/{token}")
def download_submit(request: Request, token: str) -> JSONResponse:
    """Trigger a download via Radarr or Sonarr."""
    config = request.app.state.config
    conn = get_db()

    if not _DOWNLOAD_LIMITER_POST.check(get_client_ip(request)):
        return JSONResponse({"ok": False, "error": "Too many requests"}, status_code=429)

    if len(token) > 4096:
        return JSONResponse({"ok": False, "error": "Token expired or invalid"}, status_code=410)

    payload = validate_download_token(token, config.secret_key)
    if payload is None:
        return JSONResponse({"ok": False, "error": "Token expired or invalid"}, status_code=410)

    exp_value = payload.get("exp", 0)
    if not isinstance(exp_value, (int, float)):
        return JSONResponse({"ok": False, "error": "Token expired or invalid"}, status_code=410)
    if not _mark_token_used(token, int(exp_value)):
        return JSONResponse(
            {"ok": False, "error": "This download link has already been used"},
            status_code=409,
        )

    title      = payload.get("title", "")
    media_type = payload.get("mt", "")
    tmdb_id    = payload.get("tmdb")
    email      = payload.get("email", "")
    action     = payload.get("act", "download")

    is_redownload = action == "redownload"
    audit_action  = "re_downloaded" if is_redownload else "downloaded"
    audit_detail  = (
        f"Re-downloaded by {email}" if is_redownload
        else f"Downloaded '{title}' by {email}"
    )

    try:
        if media_type == "movie":
            client = build_radarr_from_db(conn, config.secret_key)
            if not client:
                _unmark_token_used(token)
                return JSONResponse({"ok": False, "error": "Radarr not configured"}, status_code=503)

            if not tmdb_id:
                lookup = client.lookup_by_term(_url_quote(title), endpoint="/api/v3/movie/lookup")
                if not lookup:
                    _unmark_token_used(token)
                    return JSONResponse({"ok": False, "error": f"'{title}' not found in Radarr"}, status_code=404)
                tmdb_id = lookup[0].get("tmdbId")

            client.add_movie(tmdb_id, title)
            logger.info("Download token: added movie '%s' (tmdb:%s) to Radarr for %s", title, tmdb_id, email)

            log_audit(conn, title, audit_action, audit_detail)
            record_download_notification(conn, email=email, title=title, media_type="movie", tmdb_id=tmdb_id, service="radarr")
            conn.commit()

            poll_token = generate_poll_token(
                media_item_id=f"radarr:{title}",
                service="radarr",
                tmdb_id=tmdb_id,
                secret_key=config.secret_key,
            )
            return JSONResponse({
                "ok":         True,
                "message":    f"Added '{title}' to Radarr — download starting shortly",
                "service":    "radarr",
                "tmdb_id":    tmdb_id,
                "poll_token": poll_token,
            })

        else:
            client = build_sonarr_from_db(conn, config.secret_key)
            if not client:
                _unmark_token_used(token)
                return JSONResponse({"ok": False, "error": "Sonarr not configured"}, status_code=503)

            if tmdb_id:
                results = client.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")
            else:
                results = client.lookup_by_term(_url_quote(title), endpoint="/api/v3/series/lookup")
            if not results:
                _unmark_token_used(token)
                return JSONResponse({"ok": False, "error": "Series not found in Sonarr lookup"}, status_code=404)
            tvdb_id = results[0].get("tvdbId")
            if not tvdb_id:
                _unmark_token_used(token)
                return JSONResponse({"ok": False, "error": "No TVDB ID found for this series"}, status_code=422)

            client.add_series(tvdb_id, title)
            logger.info("Download token: added series '%s' (tvdb:%s) to Sonarr for %s", title, tvdb_id, email)

            log_audit(conn, title, audit_action, audit_detail)
            record_download_notification(
                conn, email=email, title=title, media_type="tv",
                tmdb_id=tmdb_id, tvdb_id=tvdb_id, service="sonarr",
            )
            conn.commit()

            poll_token = generate_poll_token(
                media_item_id=f"sonarr:{title}",
                service="sonarr",
                tmdb_id=tmdb_id,
                secret_key=config.secret_key,
            )
            return JSONResponse({
                "ok":         True,
                "message":    f"Added '{title}' to Sonarr — download starting shortly",
                "service":    "sonarr",
                "tmdb_id":    tmdb_id,
                "poll_token": poll_token,
            })

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status in (409, 422):
            service_name = "radarr" if media_type == "movie" else "sonarr"
            svc_label = "Radarr" if media_type == "movie" else "Sonarr"
            poll_token = None
            if tmdb_id:
                poll_token = generate_poll_token(
                    media_item_id=f"{service_name}:{title}",
                    service=service_name,
                    tmdb_id=tmdb_id,
                    secret_key=config.secret_key,
                )
            response: dict = {
                "ok":    False,
                "error": f"'{title}' already exists in your {svc_label} library",
            }
            if poll_token:
                response["poll_token"] = poll_token
            return JSONResponse(response, status_code=409)
        _unmark_token_used(token)
        logger.warning("Download token submit failed for '%s': %s", title, exc, exc_info=True)
        return JSONResponse({"ok": False, "error": "Download request failed — check service connectivity"}, status_code=502)
    except Exception as exc:
        _unmark_token_used(token)
        logger.warning("Download token submit failed for '%s': %s", title, exc, exc_info=True)
        return JSONResponse({"ok": False, "error": "Download request failed — check service connectivity"}, status_code=502)


def _unknown_item() -> dict:
    """Return the minimal item shape for an unknown/error state."""
    return build_item(
        dl_id="", title="", media_type="movie", poster_url="",
        state="unknown", progress=0, eta="", size_done="", size_total="",
    )


def _radarr_status(conn, secret_key: str, tmdb_id: int) -> dict:
    """Return the download-status item dict for a Radarr movie by TMDB ID."""
    client = build_radarr_from_db(conn, secret_key)
    if not client:
        return _unknown_item()

    movie = client.get_movie_by_tmdb(tmdb_id)
    if movie and movie.get("hasFile"):
        title = movie.get("title", "")
        poster_url = extract_poster_url(movie.get("images")) or ""
        return build_item(
            dl_id=f"radarr:{title}", title=title, media_type="movie",
            poster_url=poster_url, state="ready", progress=100,
            eta="", size_done="", size_total="",
        )

    queue = client.get_queue()
    for item in queue:
        item_movie = item.get("movie") or {}
        if item_movie.get("tmdbId") == tmdb_id:
            size_left  = item.get("sizeleft", 0)
            size_total = item.get("size", 0)
            progress   = (
                round((1 - size_left / size_total) * 100)
                if size_total > 0 else 0
            )
            state = map_arr_status(
                item.get("status") or "",
                item.get("trackedDownloadState") or "",
            )
            eta = _format_timeleft(item.get("timeleft", ""))
            if state == "almost_ready":
                eta = "Post-processing…"
            title = item_movie.get("title", "")
            poster_url = extract_poster_url(item_movie.get("images")) or ""
            return build_item(
                dl_id=f"radarr:{title}", title=title,
                media_type="movie", poster_url=poster_url,
                state=state, progress=progress, eta=eta,
                size_done=format_bytes(size_total - size_left),
                size_total=format_bytes(size_total),
            )

    title = (movie or {}).get("title", "")
    if title:
        recent = conn.execute(
            "SELECT dl_id, title, poster_url FROM recent_downloads WHERE dl_id = ?",
            (f"radarr:{title}",),
        ).fetchone()
        if recent:
            return build_item(
                dl_id=recent["dl_id"], title=recent["title"],
                media_type="movie", poster_url=recent["poster_url"] or "",
                state="ready", progress=100,
                eta="", size_done="", size_total="",
            )

    return build_item(
        dl_id=f"radarr:{title}" if title else "", title=title,
        media_type="movie", poster_url="", state="searching",
        progress=0, eta="", size_done="", size_total="",
    )


def _sonarr_status(conn, secret_key: str, tmdb_id: int) -> dict:
    """Return the download-status item dict for a Sonarr series by TMDB ID."""
    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        return _unknown_item()

    queue = client.get_queue()
    series_title = ""
    series_poster = ""
    ep_entries: list[dict] = []

    for item in queue:
        item_series = item.get("series") or {}
        if item_series.get("tmdbId") != tmdb_id:
            continue

        if not series_title:
            series_title = item_series.get("title", "")
        if not series_poster:
            series_poster = extract_poster_url(item_series.get("images")) or ""

        episode = item.get("episode") or {}
        size = item.get("size") or 0
        sizeleft = item.get("sizeleft") or 0
        ep_progress = round((1 - sizeleft / max(size, 1)) * 100) if size else 0
        season_num = episode.get("seasonNumber")
        ep_num = episode.get("episodeNumber")
        ep_label = fmt_episode_label(season_num, ep_num)

        ep_entries.append({
            "label": ep_label,
            "title": episode.get("title", ""),
            "progress": ep_progress,
            "size": size,
            "sizeleft": sizeleft,
            "status": item.get("status") or "",
            "tracked_state": item.get("trackedDownloadState") or "",
            "timeleft": item.get("timeleft", ""),
        })

    if ep_entries:
        ep_entries.sort(key=lambda e: e["label"])
        episodes = build_episode_dicts(ep_entries)
        total_size = sum(e["size"] for e in ep_entries)
        total_left = sum(e["sizeleft"] for e in ep_entries)
        overall_progress = (
            round((1 - total_left / max(total_size, 1)) * 100) if total_size else 0
        )
        raw_statuses = [e["status"] for e in ep_entries]
        raw_tracked = [e["tracked_state"] for e in ep_entries]
        combined_status = next(
            (s for s in raw_statuses if s.lower() in ("downloading", "completed")),
            raw_statuses[0] if raw_statuses else "",
        )
        combined_tracked = next(
            (s for s in raw_tracked if s.lower() in ("downloading", "importing", "importpending")),
            raw_tracked[0] if raw_tracked else "",
        )
        state = map_arr_status(combined_status, combined_tracked)
        eta = _format_timeleft(
            max((e["timeleft"] for e in ep_entries if e["timeleft"]), default="")
        )
        if state == "almost_ready":
            eta = "Post-processing…"
        episode_summary = build_episode_summary(episodes)
        return build_item(
            dl_id=f"sonarr:{series_title}", title=series_title,
            media_type="series", poster_url=series_poster,
            state=state, progress=overall_progress, eta=eta,
            size_done=format_bytes(total_size - total_left),
            size_total=format_bytes(total_size),
            episodes=episodes, episode_summary=episode_summary,
        )

    all_series = client.get_series()
    matched = next(
        (s for s in all_series if s.get("tmdbId") == tmdb_id),
        None,
    )
    if matched:
        stats = matched.get("statistics") or {}
        s_title = matched.get("title", "")
        if stats.get("episodeFileCount", 0) > 0:
            return build_item(
                dl_id=f"sonarr:{s_title}", title=s_title,
                media_type="series", poster_url="", state="ready",
                progress=100, eta="", size_done="", size_total="",
            )

        recent = conn.execute(
            "SELECT dl_id, title, poster_url FROM recent_downloads WHERE dl_id = ?",
            (f"sonarr:{s_title}",),
        ).fetchone()
        if recent:
            return build_item(
                dl_id=recent["dl_id"], title=recent["title"],
                media_type="series", poster_url=recent["poster_url"] or "",
                state="ready", progress=100,
                eta="", size_done="", size_total="",
            )

        return build_item(
            dl_id=f"sonarr:{s_title}", title=s_title,
            media_type="series", poster_url="", state="searching",
            progress=0, eta="", size_done="", size_total="",
        )

    return build_item(
        dl_id="", title="", media_type="series", poster_url="",
        state="searching", progress=0, eta="", size_done="",
        size_total="",
    )


@router.get("/api/download/status")
def download_status(
    request: Request,
    service: str,
    tmdb_id: int,
    token: str | None = None,
    poll_token: str | None = None,
    admin: str | None = Depends(get_optional_admin),
) -> JSONResponse:
    """Poll the download progress for a recently-requested item."""
    config = request.app.state.config

    if not _DOWNLOAD_STATUS_LIMITER.check(get_client_ip(request)):
        return JSONResponse({"error": "Too many requests"}, status_code=429)

    if not admin:
        authenticated = False

        if poll_token is not None:
            if len(poll_token) <= 4096 and validate_poll_token(
                poll_token, config.secret_key, service=service, tmdb_id=tmdb_id
            ):
                authenticated = True

        if not authenticated and token is not None:
            if len(token) > 4096:
                return JSONResponse({"error": "Not authenticated"}, status_code=401)
            payload = validate_download_token(token, config.secret_key)
            if payload is not None:
                payload_tmdb = payload.get("tmdb")
                payload_mt = payload.get("mt")
                want_service = "sonarr" if payload_mt in ("tv", "anime") else "radarr"
                if payload_tmdb == tmdb_id and service == want_service:
                    authenticated = True

        if not authenticated:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)

    conn = get_db()

    try:
        if service == "radarr":
            return JSONResponse(_radarr_status(conn, config.secret_key, tmdb_id))
        elif service == "sonarr":
            return JSONResponse(_sonarr_status(conn, config.secret_key, tmdb_id))
        else:
            return JSONResponse(_unknown_item())

    except requests.RequestException as exc:
        logger.warning("download_status error (service=%s tmdb_id=%s): %s", service, tmdb_id, exc)
        return JSONResponse(_unknown_item())
