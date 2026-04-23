"""Recommended For You page — AI-powered media recommendations."""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from datetime import date as _date, datetime, timedelta, timezone

import requests as _requests

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.middleware import (
    get_current_admin,
    get_optional_admin_from_token,
    resolve_page_session,
)
from mediaman.auth.rate_limit import ActionRateLimiter
from mediaman.crypto import generate_download_token
from mediaman.db import (
    finish_refresh_run,
    get_db,
    is_refresh_running,
    start_refresh_run,
)
from mediaman.services.arr_build import (
    build_plex_from_db,
    build_radarr_from_db,
    build_sonarr_from_db,
)
from mediaman.services.arr_state import (
    build_radarr_cache,
    build_sonarr_cache,
    compute_download_state,
)
from mediaman.services.download_notifications import record_download_notification
from mediaman.services.settings_reader import get_bool_setting, get_string_setting

logger = logging.getLogger("mediaman")

router = APIRouter()

_refresh_result: dict | None = None

# Rate-limit authenticated admin actions on the recommended endpoints.
# Both the download trigger and the share-token mint are limited to
# 30 per minute / 500 per day per admin username so a compromised
# credential or a scripted loop cannot hammer Radarr/Sonarr or pre-mint
# a warehouse of share tokens.
_DOWNLOAD_ACTION_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=500)
_SHARE_TOKEN_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=500)


def _fetch_recommendations(conn) -> list[dict]:
    """Return cached recommendations from the DB, ordered by type then insertion order."""
    rows = conn.execute("""
        SELECT id, title, year, media_type, category, tmdb_id, description, reason, poster_url, trailer_url, rating, rt_rating, tagline, runtime, genres, cast_json, director, trailer_key, imdb_rating, metascore, batch_id, downloaded_at, created_at
        FROM suggestions ORDER BY batch_id DESC, category DESC, id ASC
    """).fetchall()
    return [dict(r) for r in rows]


# Manual recommendation refreshes are throttled so a malicious or
# impatient user can't burn through OpenAI tokens by spamming the
# button (or by calling /api/recommended/refresh directly). The
# scheduled background refresh is unaffected — it runs once per scan
# and doesn't update this timestamp.
RECOMMENDATION_REFRESH_COOLDOWN_HOURS = 24
_LAST_REFRESH_KEY = "last_manual_recommendation_refresh"


def _last_manual_refresh(conn) -> datetime | None:
    val = get_string_setting(conn, _LAST_REFRESH_KEY)
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except (TypeError, ValueError):
        return None


def _refresh_cooldown_remaining(conn) -> timedelta | None:
    """Time still on the manual-refresh cooldown, or None if a new run is allowed."""
    last = _last_manual_refresh(conn)
    if last is None:
        return None
    cooldown = timedelta(hours=RECOMMENDATION_REFRESH_COOLDOWN_HOURS)
    elapsed = datetime.now(timezone.utc) - last
    if elapsed >= cooldown:
        return None
    return cooldown - elapsed


def _record_manual_refresh(conn, when: datetime) -> None:
    iso = when.isoformat()
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (_LAST_REFRESH_KEY, iso, iso),
    )
    conn.commit()


@router.get("/suggestions")
def _legacy_suggestions_redirect(request: Request) -> RedirectResponse:
    """Permanent redirect for bookmarked /suggestions URLs — auth-gated."""
    if get_optional_admin_from_token(
        request.cookies.get("session_token"), request=request
    ) is None:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/recommended", status_code=301)


@router.get("/recommended", response_class=HTMLResponse)
def recommended_page(request: Request) -> Response:
    """Render the Recommended For You page, grouping recommendations by batch into accordion sections."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    enabled = get_bool_setting(conn, "suggestions_enabled", default=True)

    recommendations = _fetch_recommendations(conn) if enabled else []

    # Group by batch_id, preserving DESC order from the query
    batches_map: OrderedDict = OrderedDict()
    for s in recommendations:
        bid = s.get("batch_id") or s.get("created_at", "")[:10]
        if bid not in batches_map:
            batches_map[bid] = {"trending": [], "personal": []}
        if s.get("category") == "trending":
            batches_map[bid]["trending"].append(s)
        else:
            batches_map[bid]["personal"].append(s)

    today = _date.today()

    def _relative_label(batch_date: _date | None, index: int) -> str:
        if index == 0:
            return "Latest picks"
        if batch_date is None:
            return "Earlier picks"
        days = (today - batch_date).days
        if days <= 0:
            return "Earlier today"
        if days == 1:
            return "Yesterday"
        if days < 7:
            return f"{days} days ago"
        if days < 14:
            return "Last week"
        weeks = days // 7
        if weeks < 5:
            return f"{weeks} weeks ago"
        months = max(1, days // 30)
        return "A month ago" if months == 1 else f"{months} months ago"

    formatted_batches = []
    for index, (bid, groups) in enumerate(list(batches_map.items())[:4]):
        try:
            batch_date: _date | None = datetime.strptime(bid, "%Y-%m-%d").date()
            date_label = batch_date.strftime("%-d %B %Y")
        except (ValueError, TypeError):
            batch_date = None
            date_label = str(bid)
        formatted_batches.append({
            "batch_id": bid,
            "date_label": date_label,
            "relative_label": _relative_label(batch_date, index),
            "is_latest": index == 0,
            "trending": groups["trending"],
            "personal": groups["personal"],
        })

    # Check library state for downloaded items.
    # Share URLs are no longer embedded in the page — they are minted on
    # demand when the user clicks the share button, via
    # POST /api/recommended/{id}/share-token.
    config = request.app.state.config

    radarr_cache: dict | None = None
    sonarr_cache: dict | None = None

    all_recs = {}
    for batch in formatted_batches:
        for item in batch["trending"] + batch["personal"]:
            if item.get("tmdb_id"):
                if item["media_type"] == "movie":
                    if radarr_cache is None:
                        radarr_cache = build_radarr_cache(build_radarr_from_db(conn, config.secret_key))
                    caches = {**radarr_cache, **build_sonarr_cache(None)}
                else:
                    if sonarr_cache is None:
                        sonarr_cache = build_sonarr_cache(build_sonarr_from_db(conn, config.secret_key))
                    caches = {**build_radarr_cache(None), **sonarr_cache}
                state = compute_download_state(item["media_type"], item["tmdb_id"], caches)
                if state is not None:
                    item["download_state"] = state

            all_recs[item["id"]] = item

    all_recommendations_json = json.dumps(all_recs, default=str).replace("</", "<\\/")

    cooldown = _refresh_cooldown_remaining(conn)
    if cooldown is None:
        manual_refresh_available = True
        next_manual_refresh_at = None
    else:
        manual_refresh_available = False
        next_manual_refresh_at = (
            datetime.now(timezone.utc) + cooldown
        ).isoformat()

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "recommended.html", {
        "username": username,
        "nav_active": "recommended",
        "batches": formatted_batches,
        "enabled": enabled,
        "all_recommendations_json": all_recommendations_json,
        "manual_refresh_available": manual_refresh_available,
        "next_manual_refresh_at": next_manual_refresh_at,
    })


@router.get("/api/recommended")
def api_recommended(admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return cached recommendations as JSON."""
    conn = get_db()
    return JSONResponse({"recommendations": _fetch_recommendations(conn)})


@router.post("/api/recommended/refresh")
def api_refresh_recommendations(request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Start a manual recommendation refresh in the background.

    Rate-limited to once per 24 hours to keep OpenAI spend bounded.
    The cooldown is enforced server-side (the UI also hides the button)
    so direct POSTs from a script can't bypass it.
    """
    conn = get_db()

    # Cooldown — enforced before we touch OpenAI / Plex / the lock.
    cooldown = _refresh_cooldown_remaining(conn)
    if cooldown is not None:
        next_at = (datetime.now(timezone.utc) + cooldown).isoformat()
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "Recommendations were already refreshed in the last "
                    f"{RECOMMENDATION_REFRESH_COOLDOWN_HOURS} hours."
                ),
                "cooldown_seconds": int(cooldown.total_seconds()),
                "next_available_at": next_at,
            },
            status_code=429,
        )

    global _refresh_result
    run_id = start_refresh_run(conn)
    if run_id is None:
        return JSONResponse({"status": "already_running"})

    config = request.app.state.config
    plex = build_plex_from_db(conn, config.secret_key)
    if not plex:
        finish_refresh_run(conn, run_id, "error", "Plex not configured")
        return JSONResponse({"ok": False, "error": "Plex not configured"})

    # Record the start time *before* the work begins so a concurrent
    # second POST is also rejected even if the first hasn't finished.
    _record_manual_refresh(conn, datetime.now(timezone.utc))

    _secret_key = config.secret_key
    _db_path = request.app.state.db_path

    def run():
        global _refresh_result
        import sqlite3
        from mediaman.db import _configure_connection

        thread_conn = sqlite3.connect(_db_path)
        _configure_connection(thread_conn)
        result: dict
        try:
            from mediaman.services.openai_recommendations import refresh_recommendations

            plex_client = build_plex_from_db(thread_conn, _secret_key)
            if plex_client:
                count = refresh_recommendations(thread_conn, plex_client, manual=True)
                result = {"ok": True, "count": count}
            else:
                result = {"ok": False, "error": "Plex not configured"}
            finish_refresh_run(thread_conn, run_id, "done")
        except Exception as exc:
            logger.exception("Background recommendation refresh failed")
            result = {"ok": False, "error": "Recommendation refresh failed"}
            try:
                finish_refresh_run(thread_conn, run_id, "error", str(exc))
            except Exception:
                pass
        finally:
            _refresh_result = result
            thread_conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return JSONResponse({"status": "started"})


@router.get("/api/recommended/refresh/status")
def api_refresh_status(admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Poll whether the background refresh is still running.

    Also returns cooldown info so the page can keep the button hidden
    after a successful refresh without needing a full reload.
    """
    conn = get_db()
    running = is_refresh_running(conn)
    result = _refresh_result
    cooldown = _refresh_cooldown_remaining(conn)
    cooldown_payload: dict = {"manual_refresh_available": cooldown is None}
    if cooldown is not None:
        cooldown_payload["cooldown_seconds"] = int(cooldown.total_seconds())
        cooldown_payload["next_available_at"] = (
            datetime.now(timezone.utc) + cooldown
        ).isoformat()

    if running:
        return JSONResponse({"status": "running", **cooldown_payload})
    if result is not None:
        return JSONResponse({"status": "done", "result": result, **cooldown_payload})
    return JSONResponse({"status": "idle", **cooldown_payload})


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

    base_url = (get_string_setting(conn, "base_url") or "").rstrip("/")
    if not base_url:
        return JSONResponse({"ok": False, "error": "Base URL not configured — cannot generate share link"})

    import time as _time
    ttl_days = 14
    expires_at_ts = int(_time.time()) + ttl_days * 86400
    from datetime import datetime, timezone as _tz
    expires_at = datetime.fromtimestamp(expires_at_ts, tz=_tz.utc).isoformat()

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
        admin, recommendation_id, row["title"],
    )
    return JSONResponse({"ok": True, "token": share_token, "share_url": share_url, "expires_at": expires_at})


@router.post("/api/recommended/{recommendation_id}/download")
def api_download_recommendation(recommendation_id: int, request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
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

    tmdb_id = row["tmdb_id"]
    if not tmdb_id:
        return JSONResponse({"ok": False, "error": "No TMDB ID — cannot add to Radarr/Sonarr"})

    try:
        if row["media_type"] == "movie":
            client = build_radarr_from_db(conn, config.secret_key)
            if not client:
                return JSONResponse({"ok": False, "error": "Radarr not configured"})
            client.add_movie(tmdb_id, row["title"])
            logger.info("Added movie '%s' (tmdb:%d) to Radarr", row["title"], tmdb_id)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE suggestions SET downloaded_at = ? WHERE id = ?",
                (now, recommendation_id),
            )
            # Get admin email for download notification
            admin_row = conn.execute(
                "SELECT email FROM subscribers WHERE active=1 LIMIT 1"
            ).fetchone()
            notify_email = admin_row["email"] if admin_row else admin
            record_download_notification(conn, email=notify_email, title=row["title"], media_type="movie", tmdb_id=tmdb_id, service="radarr")
            conn.commit()
            return JSONResponse({"ok": True, "message": f"Added '{row['title']}' to Radarr"})

        else:  # TV
            client = build_sonarr_from_db(conn, config.secret_key)
            if not client:
                return JSONResponse({"ok": False, "error": "Sonarr not configured"})
            # Sonarr lookup by TMDB ID to get TVDB ID
            results = client._get(f"/api/v3/series/lookup?term=tmdb:{tmdb_id}")
            if not results:
                return JSONResponse({"ok": False, "error": "Show not found in Sonarr lookup"})
            tvdb_id = results[0].get("tvdbId")
            if not tvdb_id:
                return JSONResponse({"ok": False, "error": "No TVDB ID found for this show"})

            client.add_series(tvdb_id, row["title"])
            logger.info("Added series '%s' (tvdb:%d) to Sonarr", row["title"], tvdb_id)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE suggestions SET downloaded_at = ? WHERE id = ?",
                (now, recommendation_id),
            )
            admin_row = conn.execute(
                "SELECT email FROM subscribers WHERE active=1 LIMIT 1"
            ).fetchone()
            notify_email = admin_row["email"] if admin_row else admin
            # Sonarr matches series by TVDB id, not TMDB — keep both so the
            # completion checker uses the right field per service.
            record_download_notification(conn, email=notify_email, title=row["title"], media_type="tv", tmdb_id=tmdb_id, tvdb_id=tvdb_id, service="sonarr")
            conn.commit()
            return JSONResponse({"ok": True, "message": f"Added '{row['title']}' to Sonarr"})

    except _requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status in (409, 422):
            return JSONResponse({"ok": False, "error": f"'{row['title']}' already exists in your library"})
        logger.warning("Failed to add recommendation '%s': HTTP %s", row["title"], status, exc_info=True)
        return JSONResponse({"ok": False, "error": "Failed to add to download queue"})
    except Exception as exc:
        logger.warning("Failed to add recommendation '%s': %s", row["title"], exc, exc_info=True)
        return JSONResponse({"ok": False, "error": "Failed to add to download queue"})
