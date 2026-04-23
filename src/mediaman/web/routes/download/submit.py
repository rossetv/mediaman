"""Download submit endpoint."""

from __future__ import annotations

import logging
from urllib.parse import quote as _url_quote

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mediaman.auth.audit import log_audit
from mediaman.auth.rate_limit import RateLimiter, get_client_ip
from mediaman.crypto import generate_poll_token, validate_download_token
from mediaman.db import get_db
from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.download_notifications import record_download_notification

from ._tokens import _mark_token_used, _unmark_token_used

logger = logging.getLogger("mediaman")

router = APIRouter()

_DOWNLOAD_LIMITER_POST = RateLimiter(max_attempts=10, window_seconds=60)


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
