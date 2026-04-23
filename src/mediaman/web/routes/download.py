"""Guest download route — token-authenticated download/re-download confirmations.

Three endpoints:
- GET  /download/{token}          — confirmation page (confirm | expired)
- POST /download/{token}          — trigger download via Radarr/Sonarr, returns JSON
- GET  /api/download/status       — poll download progress (query: service, tmdb_id)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from urllib.parse import quote as _url_quote

import requests

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from mediaman.auth.audit import log_audit
from mediaman.auth.middleware import get_optional_admin
from mediaman.services.download_notifications import record_download_notification
from mediaman.auth.rate_limit import RateLimiter, get_client_ip
from mediaman.crypto import validate_download_token
from mediaman.db import get_db
from mediaman.services.download_format import (
    build_episode_summary,
    build_item,
    fmt_episode_label,
    map_arr_status,
    map_episode_state,
    extract_poster_url,
)
from mediaman.services.format import format_bytes
from mediaman.services.download_queue import build_episode_dicts
from mediaman.services.arr_build import (
    build_radarr_from_db,
    build_sonarr_from_db,
)
from mediaman.services.item_enrichment import enrich_redownload_item

logger = logging.getLogger("mediaman")

router = APIRouter()

# Rate limiter for the public download endpoint. Higher for GET (users
# reload the confirm page), stricter for POST (the action).
_DOWNLOAD_LIMITER_GET = RateLimiter(max_attempts=30, window_seconds=60)
_DOWNLOAD_LIMITER_POST = RateLimiter(max_attempts=10, window_seconds=60)

# Status polling is called frequently by the UI — allow more but still
# cap to prevent a leaked token polling the admin's queue forever.
_DOWNLOAD_STATUS_LIMITER = RateLimiter(max_attempts=120, window_seconds=60)

# In-memory single-use cache for download-token POSTs. The store is keyed
# by the SHA-256 of the token so the raw token never sits in the process
# heap longer than one request. Entries expire with the token itself
# (14 days default), so the dict stays bounded. On process restart the
# set is lost — acceptable tradeoff: tokens are HMAC-authenticated and
# short-lived, and the downstream Radarr/Sonarr already idempotently
# handle "already in library" errors.
_USED_TOKENS_LOCK = threading.Lock()
_USED_TOKENS: dict[str, float] = {}


def _mark_token_used(token: str, exp: int) -> bool:
    """Atomically mark *token* as consumed. Return False if already used."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _USED_TOKENS_LOCK:
        # Opportunistic prune of expired entries so the dict stays bounded.
        if len(_USED_TOKENS) > 1000:
            for k, v in list(_USED_TOKENS.items()):
                if v < now:
                    _USED_TOKENS.pop(k, None)
        if digest in _USED_TOKENS:
            return False
        _USED_TOKENS[digest] = float(exp)
        return True


def _format_timeleft(timeleft: str) -> str:
    """Convert HH:MM:SS timeleft string to a human-readable eta string.

    Returns an empty string if the input is missing or malformed.
    """
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
    return f"~{secs} sec remaining"


@router.get("/download/{token}", response_class=HTMLResponse)
def download_page(request: Request, token: str) -> HTMLResponse:
    """Render the download confirmation page.

    State is one of:
    - ``"expired"``  — token is invalid, tampered, or past its TTL
    - ``"confirm"``  — valid token; show item details and a confirm button
    """
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

    item: dict = {
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

    sid = payload.get("sid")
    if sid:
        # Recommendation download — enrich from recommendations cache
        row = conn.execute(
            "SELECT poster_url, year, description, reason, rating, rt_rating, "
            "tagline, runtime, genres, cast_json, director, trailer_key, imdb_rating, metascore "
            "FROM suggestions WHERE id = ?",
            (sid,),
        ).fetchone()
        if row:
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
                "trailer_key": row["trailer_key"],
                "imdb_rating": row["imdb_rating"],
                "metascore":   row["metascore"],
            })
    elif payload.get("act") == "redownload":
        # Re-download — enrich with recommendation data or TMDB lookup
        enrich_redownload_item(item, conn, config.secret_key)

    # Parse JSON fields into lists for the template
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

    # Check if item is already in Radarr/Sonarr so we can show the right state
    item["download_state"] = None  # None = show download button
    tmdb_id = payload.get("tmdb")
    if tmdb_id:
        try:
            from mediaman.services.arr_state import (
                build_radarr_cache,
                build_sonarr_cache,
                compute_download_state,
            )
            radarr_client = build_radarr_from_db(conn, config.secret_key)
            sonarr_client = build_sonarr_from_db(conn, config.secret_key)
            radarr_cache = build_radarr_cache(radarr_client)
            sonarr_cache = build_sonarr_cache(sonarr_client)
            caches = {**radarr_cache, **sonarr_cache}
            mt = "movie" if payload.get("mt") == "movie" else "tv"
            state = compute_download_state(mt, tmdb_id, caches)
            if state is not None:
                item["download_state"] = state
        except Exception:
            logger.warning("Failed to check Arr library status for tmdb_id=%s", tmdb_id, exc_info=True)

    # When the item is already queued, build a hero_item for the shared
    # hero card partial so the progress section can be server-rendered.
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
    """Trigger a download via Radarr or Sonarr.

    Returns JSON: ``{"ok": true, "message": "...", "service": "radarr"|"sonarr", "tmdb_id": N}``
    or ``{"ok": false, "error": "..."}`` on failure.
    Returns HTTP 410 if the token is expired or invalid.
    """
    config = request.app.state.config
    conn = get_db()

    if not _DOWNLOAD_LIMITER_POST.check(get_client_ip(request)):
        return JSONResponse({"ok": False, "error": "Too many requests"}, status_code=429)

    if len(token) > 4096:
        return JSONResponse({"ok": False, "error": "Token expired or invalid"}, status_code=410)

    payload = validate_download_token(token, config.secret_key)
    if payload is None:
        return JSONResponse({"ok": False, "error": "Token expired or invalid"}, status_code=410)

    # Single-use semantics. Marks the token consumed BEFORE any outbound
    # action so a concurrent retry hits the "already used" path even if
    # Radarr/Sonarr is slow.
    exp_value = payload.get("exp", 0)
    if not isinstance(exp_value, (int, float)):
        return JSONResponse({"ok": False, "error": "Token expired or invalid"}, status_code=410)
    if not _mark_token_used(token, int(exp_value)):
        return JSONResponse(
            {"ok": False, "error": "This download link has already been used"},
            status_code=409,
        )

    title     = payload.get("title", "")
    media_type = payload.get("mt", "")
    tmdb_id   = payload.get("tmdb")
    email     = payload.get("email", "")
    action    = payload.get("act", "download")

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
                return JSONResponse({"ok": False, "error": "Radarr not configured"})

            if not tmdb_id:
                # Re-download: look up by title
                lookup = client._get(f"/api/v3/movie/lookup?term={_url_quote(title)}")
                if not lookup:
                    return JSONResponse({"ok": False, "error": f"'{title}' not found in Radarr"})
                tmdb_id = lookup[0].get("tmdbId")

            client.add_movie(tmdb_id, title)
            logger.info("Download token: added movie '%s' (tmdb:%s) to Radarr for %s", title, tmdb_id, email)

            log_audit(conn, title, audit_action, audit_detail)
            record_download_notification(conn, email=email, title=title, media_type="movie", tmdb_id=tmdb_id, service="radarr")
            conn.commit()

            return JSONResponse({
                "ok":      True,
                "message": f"Added '{title}' to Radarr — download starting shortly",
                "service": "radarr",
                "tmdb_id": tmdb_id,
            })

        else:
            # TV series — need TVDB ID via Sonarr lookup
            client = build_sonarr_from_db(conn, config.secret_key)
            if not client:
                return JSONResponse({"ok": False, "error": "Sonarr not configured"})

            if tmdb_id:
                results = client._get(f"/api/v3/series/lookup?term=tmdb:{tmdb_id}")
            else:
                results = client._get(f"/api/v3/series/lookup?term={_url_quote(title)}")
            if not results:
                return JSONResponse({"ok": False, "error": "Series not found in Sonarr lookup"})
            tvdb_id = results[0].get("tvdbId")
            if not tvdb_id:
                return JSONResponse({"ok": False, "error": "No TVDB ID found for this series"})

            client.add_series(tvdb_id, title)
            logger.info("Download token: added series '%s' (tvdb:%s) to Sonarr for %s", title, tvdb_id, email)

            log_audit(conn, title, audit_action, audit_detail)
            # Store the TVDB id against the TVDB column; preserve tmdb_id
            # too so future UI linking to TMDB still works.
            record_download_notification(
                conn, email=email, title=title, media_type="tv",
                tmdb_id=tmdb_id, tvdb_id=tvdb_id, service="sonarr",
            )
            conn.commit()

            return JSONResponse({
                "ok":      True,
                "message": f"Added '{title}' to Sonarr — download starting shortly",
                "service": "sonarr",
                "tmdb_id": tmdb_id,
            })

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status in (409, 422):
            service = "Radarr" if media_type == "movie" else "Sonarr"
            return JSONResponse({
                "ok":    False,
                "error": f"'{title}' already exists in your {service} library",
            })
        logger.warning("Download token submit failed for '%s': %s", title, exc, exc_info=True)
        return JSONResponse({"ok": False, "error": "Download request failed — check service connectivity"})
    except Exception as exc:
        logger.warning("Download token submit failed for '%s': %s", title, exc, exc_info=True)
        return JSONResponse({"ok": False, "error": "Download request failed — check service connectivity"})




def _unknown_item() -> dict:
    """Return the minimal item shape for an unknown/error state."""
    return build_item(
        dl_id="", title="", media_type="movie", poster_url="",
        state="unknown", progress=0, eta="", size_done="", size_total="",
    )


@router.get("/api/download/status")
def download_status(
    request: Request,
    service: str,
    tmdb_id: int,
    token: str | None = None,
    admin: str | None = Depends(get_optional_admin),
) -> JSONResponse:
    """Poll the download progress for a recently-requested item.

    Query parameters:
    - ``service``  — ``"radarr"`` or ``"sonarr"``
    - ``tmdb_id``  — integer TMDB ID

    Returns JSON using the simplified item shape: ``state``, ``progress``,
    ``eta``, ``size_done``, ``size_total``, ``episodes``, etc.
    State values: ``"ready"``, ``"downloading"``, ``"almost_ready"``,
    ``"searching"``, or ``"unknown"``.

    Authentication: accepts either a valid admin session cookie or a valid
    download ``token`` query parameter (for guest access from the download page).
    """
    config = request.app.state.config

    # Cheap rate limit to stop leaked tokens — and rogue admin-scoped
    # scripts (e.g. stored XSS hitting this endpoint under an admin
    # cookie) — from hammering queue state. Previously admins bypassed
    # the limiter, so an admin cookie could be weaponised to flood the
    # endpoint uncapped. 120/min per IP accommodates normal modal
    # polling (~1 call every 2–3 s) with plenty of headroom.
    if not _DOWNLOAD_STATUS_LIMITER.check(get_client_ip(request)):
        return JSONResponse({"error": "Too many requests"}, status_code=429)

    # Require either an admin session or a valid download token bound to
    # the item being queried. Without the binding check a user with any
    # valid download token could poll the queue state of arbitrary items.
    if not admin:
        if token is not None and len(token) > 4096:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        payload = validate_download_token(token, config.secret_key) if token else None
        if payload is None:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        payload_tmdb = payload.get("tmdb")
        payload_mt = payload.get("mt")
        want_service = "sonarr" if payload_mt in ("tv", "anime") else "radarr"
        if payload_tmdb != tmdb_id or service != want_service:
            return JSONResponse({"error": "Token not valid for this item"}, status_code=403)

    conn = get_db()

    try:
        if service == "radarr":
            client = build_radarr_from_db(conn, config.secret_key)
            if not client:
                return JSONResponse(_unknown_item())

            # Check if the file is already present
            movie = client.get_movie_by_tmdb(tmdb_id)
            if movie and movie.get("hasFile"):
                title = movie.get("title", "")
                poster_url = extract_poster_url(movie.get("images")) or ""
                return JSONResponse(build_item(
                    dl_id=f"radarr:{title}", title=title, media_type="movie",
                    poster_url=poster_url, state="ready", progress=100,
                    eta="", size_done="", size_total="",
                ))

            # Check the download queue for progress info
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
                        eta = "Post-processing\u2026"
                    title = item_movie.get("title", "")
                    poster_url = extract_poster_url(item_movie.get("images")) or ""
                    return JSONResponse(build_item(
                        dl_id=f"radarr:{title}", title=title,
                        media_type="movie", poster_url=poster_url,
                        state=state, progress=progress, eta=eta,
                        size_done=format_bytes(size_total - size_left),
                        size_total=format_bytes(size_total),
                    ))

            # Check recent_downloads — the movie may have completed already
            title = (movie or {}).get("title", "")
            if title:
                recent = conn.execute(
                    "SELECT dl_id, title, poster_url FROM recent_downloads WHERE dl_id = ?",
                    (f"radarr:{title}",),
                ).fetchone()
                if recent:
                    return JSONResponse(build_item(
                        dl_id=recent["dl_id"], title=recent["title"],
                        media_type="movie", poster_url=recent["poster_url"] or "",
                        state="ready", progress=100,
                        eta="", size_done="", size_total="",
                    ))

            # Movie exists in Radarr but nothing in the queue — still searching
            return JSONResponse(build_item(
                dl_id=f"radarr:{title}" if title else "", title=title,
                media_type="movie", poster_url="", state="searching",
                progress=0, eta="", size_done="", size_total="",
            ))

        elif service == "sonarr":
            client = build_sonarr_from_db(conn, config.secret_key)
            if not client:
                return JSONResponse(_unknown_item())

            # Check the download queue and group episodes for this series
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
                # Aggregate state: pick the most advanced status
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
                # ETA from the episode with the most timeleft
                eta = _format_timeleft(
                    max((e["timeleft"] for e in ep_entries if e["timeleft"]), default="")
                )
                if state == "almost_ready":
                    eta = "Post-processing\u2026"
                episode_summary = build_episode_summary(episodes)
                return JSONResponse(build_item(
                    dl_id=f"sonarr:{series_title}", title=series_title,
                    media_type="series", poster_url=series_poster,
                    state=state, progress=overall_progress, eta=eta,
                    size_done=format_bytes(total_size - total_left),
                    size_total=format_bytes(total_size),
                    episodes=episodes, episode_summary=episode_summary,
                ))

            # Nothing in queue — check if series has any files already
            all_series = client.get_series()
            matched = next(
                (s for s in all_series if s.get("tmdbId") == tmdb_id),
                None,
            )
            if matched:
                stats = matched.get("statistics") or {}
                s_title = matched.get("title", "")
                if stats.get("episodeFileCount", 0) > 0:
                    return JSONResponse(build_item(
                        dl_id=f"sonarr:{s_title}", title=s_title,
                        media_type="series", poster_url="", state="ready",
                        progress=100, eta="", size_done="", size_total="",
                    ))

                # Check recent_downloads before falling through to "searching"
                recent = conn.execute(
                    "SELECT dl_id, title, poster_url FROM recent_downloads WHERE dl_id = ?",
                    (f"sonarr:{s_title}",),
                ).fetchone()
                if recent:
                    return JSONResponse(build_item(
                        dl_id=recent["dl_id"], title=recent["title"],
                        media_type="series", poster_url=recent["poster_url"] or "",
                        state="ready", progress=100,
                        eta="", size_done="", size_total="",
                    ))

                # Series in Sonarr but no episodes downloading or downloaded
                return JSONResponse(build_item(
                    dl_id=f"sonarr:{s_title}", title=s_title,
                    media_type="series", poster_url="", state="searching",
                    progress=0, eta="", size_done="", size_total="",
                ))

            return JSONResponse(build_item(
                dl_id="", title="", media_type="series", poster_url="",
                state="searching", progress=0, eta="", size_done="",
                size_total="",
            ))

        else:
            return JSONResponse(_unknown_item())

    except requests.RequestException as exc:
        logger.warning("download_status error (service=%s tmdb_id=%s): %s", service, tmdb_id, exc)
        return JSONResponse(_unknown_item())
