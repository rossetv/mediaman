"""Download status polling endpoint."""

from __future__ import annotations

import logging
import sqlite3

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.auth.middleware import get_optional_admin
from mediaman.auth.rate_limit import RateLimiter, get_client_ip
from mediaman.crypto import validate_poll_token
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.download_format import (
    build_episode_summary,
    build_item,
    extract_poster_url,
    format_episode_label,
    map_arr_status,
)
from mediaman.services.downloads.download_queue import build_episode_dicts
from mediaman.services.infra.format import format_bytes

logger = logging.getLogger("mediaman")

router = APIRouter()

_DOWNLOAD_STATUS_LIMITER = RateLimiter(max_attempts=120, window_seconds=60)


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


_UNKNOWN_ITEM: dict[str, object] = build_item(
    dl_id="",
    title="",
    media_type="movie",
    poster_url="",
    state="unknown",
    progress=0,
    eta="",
    size_done="",
    size_total="",
)


def _radarr_status(conn: sqlite3.Connection, secret_key: str, tmdb_id: int) -> dict[str, object]:
    """Return the download-status item dict for a Radarr movie by TMDB ID."""
    client = build_radarr_from_db(conn, secret_key)
    if not client:
        return _UNKNOWN_ITEM

    movie = client.get_movie_by_tmdb(tmdb_id)
    if movie and movie.get("hasFile"):
        title = movie.get("title", "")
        poster_url = extract_poster_url(movie.get("images"))
        return build_item(
            dl_id=f"radarr:{title}",
            title=title,
            media_type="movie",
            poster_url=poster_url,
            state="ready",
            progress=100,
            eta="",
            size_done="",
            size_total="",
        )

    queue = client.get_queue()
    for item in queue:
        item_movie = item.get("movie") or {}
        if item_movie.get("tmdbId") == tmdb_id:
            size_left = item.get("sizeleft", 0)
            size_total = item.get("size", 0)
            progress = round((1 - size_left / size_total) * 100) if size_total > 0 else 0
            state = map_arr_status(
                item.get("status") or "",
                item.get("trackedDownloadState") or "",
            )
            eta = _format_timeleft(item.get("timeleft", ""))
            if state == "almost_ready":
                eta = "Post-processing…"
            title = item_movie.get("title", "")
            poster_url = extract_poster_url(item_movie.get("images"))
            return build_item(
                dl_id=f"radarr:{title}",
                title=title,
                media_type="movie",
                poster_url=poster_url,
                state=state,
                progress=progress,
                eta=eta,
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
                dl_id=recent["dl_id"],
                title=recent["title"],
                media_type="movie",
                poster_url=recent["poster_url"] or "",
                state="ready",
                progress=100,
                eta="",
                size_done="",
                size_total="",
            )

    return build_item(
        dl_id=f"radarr:{title}" if title else "",
        title=title,
        media_type="movie",
        poster_url="",
        state="searching",
        progress=0,
        eta="",
        size_done="",
        size_total="",
    )


def _sonarr_status(conn: sqlite3.Connection, secret_key: str, tmdb_id: int) -> dict[str, object]:
    """Return the download-status item dict for a Sonarr series by TMDB ID."""
    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        return _UNKNOWN_ITEM

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
            series_poster = extract_poster_url(item_series.get("images"))

        episode = item.get("episode") or {}
        size = item.get("size") or 0
        sizeleft = item.get("sizeleft") or 0
        ep_progress = round((1 - sizeleft / max(size, 1)) * 100) if size else 0
        season_num = episode.get("seasonNumber")
        ep_num = episode.get("episodeNumber")
        ep_label = format_episode_label(season_num, ep_num)

        ep_entries.append(
            {
                "label": ep_label,
                "title": episode.get("title", ""),
                "progress": ep_progress,
                "size": size,
                "sizeleft": sizeleft,
                "status": item.get("status") or "",
                "tracked_state": item.get("trackedDownloadState") or "",
                "timeleft": item.get("timeleft", ""),
            }
        )

    if ep_entries:
        ep_entries.sort(key=lambda e: e["label"])
        episodes = build_episode_dicts(ep_entries)
        total_size = sum(e["size"] for e in ep_entries)
        total_left = sum(e["sizeleft"] for e in ep_entries)
        overall_progress = round((1 - total_left / max(total_size, 1)) * 100) if total_size else 0
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
            dl_id=f"sonarr:{series_title}",
            title=series_title,
            media_type="series",
            poster_url=series_poster,
            state=state,
            progress=overall_progress,
            eta=eta,
            size_done=format_bytes(total_size - total_left),
            size_total=format_bytes(total_size),
            episodes=episodes,
            episode_summary=episode_summary,
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
                dl_id=f"sonarr:{s_title}",
                title=s_title,
                media_type="series",
                poster_url="",
                state="ready",
                progress=100,
                eta="",
                size_done="",
                size_total="",
            )

        recent = conn.execute(
            "SELECT dl_id, title, poster_url FROM recent_downloads WHERE dl_id = ?",
            (f"sonarr:{s_title}",),
        ).fetchone()
        if recent:
            return build_item(
                dl_id=recent["dl_id"],
                title=recent["title"],
                media_type="series",
                poster_url=recent["poster_url"] or "",
                state="ready",
                progress=100,
                eta="",
                size_done="",
                size_total="",
            )

        return build_item(
            dl_id=f"sonarr:{s_title}",
            title=s_title,
            media_type="series",
            poster_url="",
            state="searching",
            progress=0,
            eta="",
            size_done="",
            size_total="",
        )

    return build_item(
        dl_id="",
        title="",
        media_type="series",
        poster_url="",
        state="searching",
        progress=0,
        eta="",
        size_done="",
        size_total="",
    )


@router.get("/api/download/status")
def download_status(
    request: Request,
    service: str,
    tmdb_id: int,
    poll_token: str | None = None,
    admin: str | None = Depends(get_optional_admin),
) -> JSONResponse:
    """Poll the download progress for a recently-requested item.

    Finding 14: unauthenticated callers must supply a ``poll_token``
    (short-lived, service/tmdb-bound) returned by the submit endpoint.
    Authenticated admins may poll without a token.
    """
    config = request.app.state.config

    if not _DOWNLOAD_STATUS_LIMITER.check(get_client_ip(request)):
        return JSONResponse({"error": "Too many requests"}, status_code=429)

    if not admin:
        # Finding 14: unauthenticated polling must use a short-lived
        # poll_token.  The long-lived download token is no longer accepted
        # for status polling — it is single-use and only valid for the
        # /download/{token} POST.  Clients receive a poll_token in the
        # submit response and must use it exclusively for polling.
        authenticated = False

        if poll_token is not None:
            if len(poll_token) <= 4096:
                poll_payload = validate_poll_token(poll_token, config.secret_key)
                if (
                    poll_payload is not None
                    and poll_payload.get("svc") == service
                    and poll_payload.get("tmdb") == tmdb_id
                ):
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
            return JSONResponse(_UNKNOWN_ITEM)

    except requests.RequestException as exc:
        logger.warning("download_status error (service=%s tmdb_id=%s): %s", service, tmdb_id, exc)
        return JSONResponse(_UNKNOWN_ITEM)
