"""Download submit endpoint."""

from __future__ import annotations

import logging
import math
import sqlite3
from typing import TypedDict

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mediaman.audit import log_audit
from mediaman.crypto import generate_poll_token, validate_download_token
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.services.infra.http import SafeHTTPError
from mediaman.services.rate_limit import RateLimiter, get_client_ip

from ._tokens import _mark_token_used, _unmark_token_used

logger = logging.getLogger("mediaman")

router = APIRouter()

_DOWNLOAD_LIMITER_POST = RateLimiter(max_attempts=10, window_seconds=60)


class DownloadPayload(TypedDict):
    """Validated parameters for a single Radarr/Sonarr download submission."""

    conn: sqlite3.Connection
    token: str
    title: str
    tmdb_id: int | None
    email: str
    audit_action: str
    audit_detail: str
    secret_key: str


def _record_and_respond(
    *,
    conn,
    email: str,
    title: str,
    media_type: str,
    tmdb_id,
    service: str,
    audit_action: str,
    audit_detail: str,
    secret_key: str,
    tvdb_id=None,
) -> JSONResponse:
    """Audit, notify, commit, mint a poll token, and return the success response.

    Shared epilogue for :func:`_submit_to_radarr` and :func:`_submit_to_sonarr`
    — the ~25 lines that are identical between the two after the Arr client
    call succeeds.
    """
    log_audit(conn, title, audit_action, audit_detail)
    record_download_notification(
        conn,
        email=email,
        title=title,
        media_type=media_type,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        service=service,
    )
    conn.commit()

    poll_token = generate_poll_token(
        media_item_id=f"{service}:{title}",
        service=service,
        tmdb_id=tmdb_id,
        secret_key=secret_key,
    )
    service_label = "Radarr" if service == "radarr" else "Sonarr"
    return JSONResponse(
        {
            "ok": True,
            "message": f"Added '{title}' to {service_label} — download starting shortly",
            "service": service,
            "tmdb_id": tmdb_id,
            "poll_token": poll_token,
        }
    )


def _submit_to_radarr(payload: DownloadPayload) -> JSONResponse:
    """Add a movie to Radarr and return the poll-token response.

    Returns a JSONResponse directly.  Raises :exc:`SafeHTTPError` on Arr-level
    failures so the caller's except clause can handle 409/422 uniformly.
    """
    conn = payload["conn"]
    token = payload["token"]
    title = payload["title"]
    tmdb_id = payload["tmdb_id"]
    email = payload["email"]
    secret_key = payload["secret_key"]

    if not tmdb_id:
        # Finding 15: never resolve a public download link by title alone — an
        # ambiguous or remade title can enqueue the wrong film.  Require a
        # stable TMDB identifier; the token is released so a corrected link
        # (with an identifier) can be issued.
        _unmark_token_used(token)
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "Missing stable media identifier — this download link cannot "
                    "be used.  Ask the admin to re-issue it with a TMDB ID."
                ),
            },
            status_code=422,
        )

    client = build_radarr_from_db(conn, secret_key)
    if not client:
        _unmark_token_used(token)
        return JSONResponse({"ok": False, "error": "Radarr not configured"}, status_code=503)

    client.add_movie(tmdb_id, title)
    logger.info(
        "Download token: added movie '%s' (tmdb:%s) to Radarr for %s", title, tmdb_id, email
    )

    return _record_and_respond(
        conn=conn,
        email=email,
        title=title,
        media_type="movie",
        tmdb_id=tmdb_id,
        service="radarr",
        audit_action=payload["audit_action"],
        audit_detail=payload["audit_detail"],
        secret_key=secret_key,
    )


def _submit_to_sonarr(payload: DownloadPayload) -> JSONResponse:
    """Add a series to Sonarr and return the poll-token response.

    Returns a JSONResponse directly.  Raises :exc:`SafeHTTPError` on Arr-level
    failures so the caller's except clause can handle 409/422 uniformly.
    """
    conn = payload["conn"]
    token = payload["token"]
    title = payload["title"]
    tmdb_id = payload["tmdb_id"]
    email = payload["email"]
    secret_key = payload["secret_key"]

    if not tmdb_id:
        # Finding 15: refuse public Sonarr submissions without a stable TMDB
        # identifier — title-only lookup_by_term can enqueue the wrong show.
        _unmark_token_used(token)
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "Missing stable media identifier — this download link cannot "
                    "be used.  Ask the admin to re-issue it with a TMDB ID."
                ),
            },
            status_code=422,
        )

    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        _unmark_token_used(token)
        return JSONResponse({"ok": False, "error": "Sonarr not configured"}, status_code=503)

    results = client.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")
    if not results:
        _unmark_token_used(token)
        return JSONResponse(
            {"ok": False, "error": "Series not found in Sonarr lookup"}, status_code=404
        )
    tvdb_id = results[0].get("tvdbId")
    if not tvdb_id:
        _unmark_token_used(token)
        return JSONResponse(
            {"ok": False, "error": "No TVDB ID found for this series"}, status_code=422
        )

    client.add_series(tvdb_id, title)
    logger.info(
        "Download token: added series '%s' (tvdb:%s) to Sonarr for %s", title, tvdb_id, email
    )

    return _record_and_respond(
        conn=conn,
        email=email,
        title=title,
        media_type="tv",
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        service="sonarr",
        audit_action=payload["audit_action"],
        audit_detail=payload["audit_detail"],
        secret_key=secret_key,
    )


# rationale: HMAC token verification, scheduled-action lookup, Arr dispatch,
# audit-log write, and scheduled-action completion form one CSRF-exempt flow
# that touches a single DB connection in strict sequence — splitting the Arr
# dispatch from the audit and completion steps would leave scheduled-action
# rows open if the later writes fail.
@router.post("/download/{token}")
def download_submit(request: Request, token: str) -> JSONResponse:
    """Trigger a download via Radarr or Sonarr.

    CSRF-exempt: this route is HMAC-token-authenticated and gets clicked
    through from email clients where the browser's Origin is whichever
    webmail host the recipient happens to use.  The exemption is opt-in
    via the explicit ``_CSRF_EXEMPT_ROUTES`` allowlist in
    :mod:`mediaman.web` — adding a sibling ``POST /download/...`` will
    NOT silently inherit the exemption.
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

    exp_value = payload.get("exp", 0)
    # ``int(float('inf'))`` raises OverflowError. Guard against a
    # signer-controlled non-finite ``exp`` so a malformed token can never
    # crash the handler — treat it as expired/invalid.
    if not isinstance(exp_value, (int, float)) or not math.isfinite(exp_value):
        return JSONResponse({"ok": False, "error": "Token expired or invalid"}, status_code=410)

    # Phase 1 of the 2-phase reservation: claim the token in the DB
    # *before* doing any Arr work. _mark_token_used returns False when
    # the digest is already recorded (replay or cross-worker collision)
    # and raises on DB failure (fail closed → 503 so the user can retry
    # once the DB recovers, rather than letting a replay slip through
    # on an unverified cache entry).
    try:
        claimed = _mark_token_used(token, int(exp_value))
    except Exception:
        # The token persistence layer already logs CRITICAL with a
        # traceback; here we only need to translate the failure into a
        # retryable response to the client.
        return JSONResponse(
            {"ok": False, "error": "Service temporarily unavailable, please retry"},
            status_code=503,
        )
    if not claimed:
        return JSONResponse(
            {"ok": False, "error": "This download link has already been used"},
            status_code=409,
        )

    title = payload.get("title", "")
    media_type = payload.get("mt", "")
    tmdb_id = payload.get("tmdb")
    email = payload.get("email", "")
    action = payload.get("act", "download")

    is_redownload = action == "redownload"
    audit_action = "re_downloaded" if is_redownload else "downloaded"
    audit_detail = (
        f"Re-downloaded by {email}" if is_redownload else f"Downloaded '{title}' by {email}"
    )

    dl_payload: DownloadPayload = {
        "conn": conn,
        "token": token,
        "title": title,
        "tmdb_id": tmdb_id,
        "email": email,
        "audit_action": audit_action,
        "audit_detail": audit_detail,
        "secret_key": config.secret_key,
    }

    try:
        if media_type == "movie":
            result = _submit_to_radarr(dl_payload)
        else:
            result = _submit_to_sonarr(dl_payload)
        # Phase 2 (success): the Arr submission completed — leave the
        # reservation row in place so the token cannot be replayed.
        return result

    except SafeHTTPError as exc:
        status = exc.status_code
        if status in (409, 422):
            # The Arr service reports the item already exists. For a
            # *re-download* link this is the expected hot path — the
            # user explicitly wants to re-grab a title that's already
            # in the library, so the token must be released so the
            # page can immediately re-issue the request via the usual
            # flow rather than leaving the user stranded with a "link
            # already used" error on the next click.
            #
            # For a fresh download link, the same response means the
            # admin already added it elsewhere; the click was
            # effectively idempotent, so we keep the token consumed
            # (preserves replay protection) and surface a poll_token
            # for the page to display the existing library state.
            if is_redownload:
                _unmark_token_used(token)
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
            response: dict[str, object] = {
                "ok": False,
                "error": f"'{title}' already exists in your {svc_label} library",
            }
            if poll_token:
                response["poll_token"] = poll_token
            return JSONResponse(response, status_code=409)
        # Phase 2 (failure): release the reservation so the user can
        # retry once the upstream recovers.
        _unmark_token_used(token)
        # Demote to DEBUG: every transient Arr blip would otherwise
        # spam the WARNING log with a full traceback. The audit trail
        # captures the user-facing failure separately, so operators
        # who need the stack can dial up the log level.
        logger.debug("Download token submit failed for '%s': %s", title, exc, exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "Download request failed — check service connectivity"},
            status_code=502,
        )
    except (requests.RequestException, sqlite3.Error) as exc:
        # Narrow handler: only swallow network/DB-shaped failures so
        # asyncio.CancelledError, KeyboardInterrupt, and other control
        # exceptions propagate as intended. The previous bare
        # ``except Exception`` masked them and silently turned an
        # in-flight cancellation into a stale 502.
        _unmark_token_used(token)
        logger.debug("Download token submit failed for '%s': %s", title, exc, exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "Download request failed — check service connectivity"},
            status_code=502,
        )
