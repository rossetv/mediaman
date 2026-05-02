"""JSON API endpoints for recommendations: listing, share-token mint, download trigger."""

from __future__ import annotations

import logging
import re
import sqlite3
import time as _time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import ActionRateLimiter
from mediaman.crypto import generate_download_token
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.services.infra.http_client import SafeHTTPError
from mediaman.services.infra.settings_reader import get_string_setting
from mediaman.services.infra.time import now_iso

from ._query import fetch_recommendations

logger = logging.getLogger("mediaman")

router = APIRouter()

# Rate-limit authenticated admin actions on the recommended endpoints.
# Both the download trigger and the share-token mint are limited to
# 30 per minute / 500 per day per admin username so a compromised
# credential or a scripted loop cannot hammer Radarr/Sonarr or pre-mint
# a warehouse of share tokens.
_DOWNLOAD_ACTION_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=500)
_SHARE_TOKEN_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=500)


def reset_download_action_limiter() -> None:
    """Clear the download-action rate-limiter state. Used by tests."""
    _DOWNLOAD_ACTION_LIMITER.reset()


def reset_share_token_limiter() -> None:
    """Clear the share-token rate-limiter state. Used by tests."""
    _SHARE_TOKEN_LIMITER.reset()


# Default and maximum slice for /api/recommended pagination (finding 22).
# A heavy backlog (months of refreshes × 4 batches × ~24 items) can balloon
# to several hundred suggestions; serialising that into one JSON payload
# blocks the page until the whole table is ready. The defaults here keep
# the page fast and let an admin paginate explicitly via ?limit&?offset.
_RECOMMENDED_LIMIT_DEFAULT = 50
_RECOMMENDED_LIMIT_MAX = 200


@router.get("/api/recommended")
def api_recommended(
    admin: str = Depends(get_current_admin),
    limit: int = Query(default=_RECOMMENDED_LIMIT_DEFAULT, ge=1, le=_RECOMMENDED_LIMIT_MAX),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> JSONResponse:
    """Return cached recommendations as JSON, paginated.

    ``limit`` defaults to 50, capped at 200; ``offset`` is bounded at
    10,000 to prevent cheap O(n) skips through a corrupted client. The
    response includes the full ordered slice plus the total so the UI
    can show "showing X of Y" without a second round-trip.
    """
    conn = get_db()
    all_recs = fetch_recommendations(conn)
    total = len(all_recs)
    sliced = all_recs[offset : offset + limit]
    return JSONResponse(
        {
            "recommendations": sliced,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.post("/api/recommended/{recommendation_id}/share-token")
def api_share_token(
    recommendation_id: int,
    request: Request,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Mint a single-use download share token for one recommendation, on demand.

    Returns ``{"token": "...", "share_url": "...", "expires_at": "..."}``.
    Rate-limited to 30/min, 500/day per admin.

    Tokens are not pre-embedded in the page (that approach leaked a stack
    of tokens to any page viewer). Instead the browser calls this endpoint
    when the user explicitly clicks the share button, and the returned
    URL is used once for copy-to-clipboard or immediate navigation.
    """
    if not _SHARE_TOKEN_LIMITER.check(admin):
        return JSONResponse({"ok": False, "error": "Too many requests"}, status_code=429)

    conn = get_db()
    config = request.app.state.config

    row = conn.execute(
        "SELECT id, title, media_type, tmdb_id FROM suggestions WHERE id = ?",
        (recommendation_id,),
    ).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "Recommendation not found"}, status_code=404)

    # Finding 15: refuse to mint a public download token unless a stable
    # TMDB identifier is present.  Without it the token is bound only to a
    # title string which can be duplicated, re-used, or spoofed.
    if not row["tmdb_id"]:
        return JSONResponse(
            {
                "ok": False,
                "error": "Cannot generate share link — no TMDB identifier for this recommendation",
            },
            status_code=422,
        )

    base_url = (get_string_setting(conn, "base_url") or "").rstrip("/")
    if not base_url:
        # Finding 24: a missing base_url is a *server-side* config
        # problem, not a client-side error — return 503 so monitoring
        # alerts on it instead of treating the empty body as a normal
        # response.
        return JSONResponse(
            {"ok": False, "error": "Base URL not configured — cannot generate share link"},
            status_code=503,
        )

    ttl_days = 14
    expires_at_ts = int(_time.time()) + ttl_days * 86400
    expires_at = datetime.fromtimestamp(expires_at_ts, tz=UTC).isoformat()

    share_token = generate_download_token(
        email=admin,
        action="download",
        title=row["title"],
        media_type=row["media_type"],
        tmdb_id=row["tmdb_id"],
        recommendation_id=row["id"],
        secret_key=config.secret_key,
        ttl_days=ttl_days,
    )
    share_url = f"{base_url}/download/{share_token}"

    logger.info(
        "Share token minted by admin '%s' for recommendation_id=%d title='%s'",
        admin,
        recommendation_id,
        row["title"],
    )
    return JSONResponse(
        {"ok": True, "token": share_token, "share_url": share_url, "expires_at": expires_at}
    )


# Defence-in-depth title sanitiser (finding 23). The canonical place to
# strip control characters is on persist (in
# ``services/openai/recommendations/persist.py`` — out of this wave's
# scope), but persisted rows from earlier OpenAI runs may still contain
# them. Sanitising at use-time means we never pass a name with embedded
# CR/LF/NUL into Arr's add_movie / add_series — those characters could
# bleed into the eventual Radarr filename or queue label.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitise_title(title: str | None) -> str:
    """Strip control characters and trim whitespace from *title*.

    Returns an empty string if *title* is ``None``. Truncates to 256
    chars to keep the eventual Arr request under any reasonable length
    cap — the canonical title was already validated on the search /
    redownload paths; this is the recommendations-side belt-and-braces.
    """
    if not title:
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", title).strip()
    return cleaned[:256]


def _add_rec_to_radarr(
    conn: sqlite3.Connection,
    *,
    admin: str,
    row: sqlite3.Row,
    recommendation_id: int,
    secret_key: str,
) -> JSONResponse:
    """Add a movie recommendation to Radarr, record notification, and return response."""
    client = build_radarr_from_db(conn, secret_key)
    if not client:
        return JSONResponse({"ok": False, "error": "Radarr not configured"})
    tmdb_id = row["tmdb_id"]
    safe_title = _sanitise_title(row["title"])
    client.add_movie(tmdb_id, safe_title)
    logger.info("Added movie '%s' (tmdb:%d) to Radarr", safe_title, tmdb_id)
    conn.execute(
        "UPDATE suggestions SET downloaded_at = ? WHERE id = ?",
        (now_iso(), recommendation_id),
    )
    # H24: notify the authenticated admin, not an arbitrary subscriber.
    record_download_notification(
        conn,
        email=admin,
        title=safe_title,
        media_type="movie",
        tmdb_id=tmdb_id,
        service="radarr",
    )
    conn.commit()
    return JSONResponse({"ok": True, "message": f"Added '{safe_title}' to Radarr"})


def _add_rec_to_sonarr(
    conn: sqlite3.Connection,
    *,
    admin: str,
    row: sqlite3.Row,
    recommendation_id: int,
    secret_key: str,
) -> JSONResponse:
    """Add a TV recommendation to Sonarr, record notification, and return response."""
    client = build_sonarr_from_db(conn, secret_key)
    if not client:
        return JSONResponse({"ok": False, "error": "Sonarr not configured"})
    tmdb_id = row["tmdb_id"]
    safe_title = _sanitise_title(row["title"])
    # Sonarr lookup by TMDB ID to get the authoritative TVDB ID
    results = client.lookup_by_tmdb_id(tmdb_id, endpoint="/api/v3/series/lookup")
    if not results:
        return JSONResponse({"ok": False, "error": "Show not found in Sonarr lookup"})
    tvdb_id = results[0].get("tvdbId")
    if not tvdb_id:
        return JSONResponse({"ok": False, "error": "No TVDB ID found for this show"})
    client.add_series(tvdb_id, safe_title)
    logger.info("Added series '%s' (tvdb:%d) to Sonarr", safe_title, tvdb_id)
    conn.execute(
        "UPDATE suggestions SET downloaded_at = ? WHERE id = ?",
        (now_iso(), recommendation_id),
    )
    # H24: notify the authenticated admin, not an arbitrary subscriber.
    # Sonarr matches series by TVDB id, not TMDB — keep both so the
    # completion checker uses the right field per service.
    record_download_notification(
        conn,
        email=admin,
        title=safe_title,
        media_type="tv",
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        service="sonarr",
    )
    conn.commit()
    return JSONResponse({"ok": True, "message": f"Added '{safe_title}' to Sonarr"})


@router.post("/api/recommended/{recommendation_id}/download")
def api_download_recommendation(
    recommendation_id: int, request: Request, admin: str = Depends(get_current_admin)
) -> JSONResponse:
    """Add a recommended movie/show to Radarr or Sonarr and trigger download.

    Rate-limited to 30/min, 500/day per admin username to prevent a
    compromised credential from hammering Radarr/Sonarr with burst requests.
    """
    if not _DOWNLOAD_ACTION_LIMITER.check(admin):
        return JSONResponse({"ok": False, "error": "Too many requests"}, status_code=429)

    conn = get_db()
    config = request.app.state.config

    row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (recommendation_id,)).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "Recommendation not found"}, status_code=404)

    if not row["tmdb_id"]:
        return JSONResponse({"ok": False, "error": "No TMDB ID — cannot add to Radarr/Sonarr"})

    try:
        if row["media_type"] == "movie":
            return _add_rec_to_radarr(
                conn,
                admin=admin,
                row=row,
                recommendation_id=recommendation_id,
                secret_key=config.secret_key,
            )
        return _add_rec_to_sonarr(
            conn,
            admin=admin,
            row=row,
            recommendation_id=recommendation_id,
            secret_key=config.secret_key,
        )

    except SafeHTTPError as exc:
        if exc.status_code in (409, 422):
            # Finding 25: surface the conflict properly — returning 200
            # made downstream clients treat "already exists" as a
            # success path, masking the real state. 409 is the standard
            # response for "the resource already exists".
            return JSONResponse(
                {"ok": False, "error": f"'{row['title']}' already exists in your library"},
                status_code=409,
            )
        logger.warning(
            "Failed to add recommendation '%s': HTTP %s",
            row["title"],
            exc.status_code,
            exc_info=True,
        )
        return JSONResponse(
            {"ok": False, "error": "Failed to add to download queue"},
            status_code=502,
        )
    except Exception as exc:
        logger.warning("Failed to add recommendation '%s': %s", row["title"], exc, exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "Failed to add to download queue"},
            status_code=502,
        )
