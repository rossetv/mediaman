"""Recommended For You page — AI-powered media recommendations."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mediaman.auth.middleware import get_current_admin
from mediaman.auth.session import validate_session
from mediaman.db import get_db

logger = logging.getLogger("mediaman")

router = APIRouter()


def _fetch_recommendations(conn) -> list[dict]:
    """Return cached recommendations from the DB, ordered by type then insertion order."""
    rows = conn.execute("""
        SELECT id, title, year, media_type, category, tmdb_id, description, reason, poster_url, trailer_url, rating, rt_rating, tagline, runtime, genres, cast_json, director, trailer_key, imdb_rating, metascore, batch_id, downloaded_at, created_at
        FROM suggestions ORDER BY batch_id DESC, category DESC, id ASC
    """).fetchall()
    return [dict(r) for r in rows]


@router.get("/suggestions")
def _legacy_suggestions_redirect():
    """Permanent redirect for bookmarked /suggestions URLs."""
    return RedirectResponse("/recommended", status_code=301)


@router.get("/recommended", response_class=HTMLResponse)
def recommended_page(request: Request):
    """Render the Recommended For You page, grouping recommendations by batch into accordion sections."""
    token = request.cookies.get("session_token")
    if not token:
        return RedirectResponse("/login", status_code=302)

    conn = get_db()
    username = validate_session(conn, token)
    if username is None:
        return RedirectResponse("/login", status_code=302)

    enabled_row = conn.execute(
        "SELECT value FROM settings WHERE key='suggestions_enabled'"
    ).fetchone()
    enabled = not enabled_row or enabled_row["value"] != "false"

    recommendations = _fetch_recommendations(conn) if enabled else []

    # Group by batch_id, preserving DESC order from the query
    from collections import OrderedDict
    from datetime import datetime as dt

    batches_map: OrderedDict = OrderedDict()
    for s in recommendations:
        bid = s.get("batch_id") or s.get("created_at", "")[:10]
        if bid not in batches_map:
            batches_map[bid] = {"trending": [], "personal": []}
        if s.get("category") == "trending":
            batches_map[bid]["trending"].append(s)
        else:
            batches_map[bid]["personal"].append(s)

    formatted_batches = []
    for bid, groups in list(batches_map.items())[:4]:
        try:
            d = dt.strptime(bid, "%Y-%m-%d")
            label = f"Recommendations · {d.strftime('%-d %B %Y')}"
        except (ValueError, TypeError):
            label = f"Recommendations · {bid}"
        formatted_batches.append({
            "batch_id": bid,
            "label": label,
            "trending": groups["trending"],
            "personal": groups["personal"],
        })

    # Generate share URLs and check library state for downloaded items
    import json
    from mediaman.crypto import generate_download_token

    config = request.app.state.config
    base_url_row = conn.execute("SELECT value FROM settings WHERE key='base_url'").fetchone()
    base_url = (base_url_row["value"] if base_url_row else "").rstrip("/")

    # Compute library state via the shared helper.
    from mediaman.services.arr_state import (
        build_radarr_cache, build_sonarr_cache, compute_download_state,
    )
    from mediaman.web.routes.download import _build_radarr, _build_sonarr

    radarr_cache: dict | None = None
    sonarr_cache: dict | None = None

    all_recs = {}
    for batch in formatted_batches:
        for item in batch["trending"] + batch["personal"]:
            # Share URL (unchanged).
            if base_url:
                item["share_url"] = "{}/download/{}".format(
                    base_url,
                    generate_download_token(
                        email=username, action="download", title=item["title"],
                        media_type=item["media_type"], tmdb_id=item.get("tmdb_id"),
                        recommendation_id=item.get("id"), secret_key=config.secret_key,
                    ),
                )
            else:
                item["share_url"] = ""

            if item.get("tmdb_id"):
                if item["media_type"] == "movie":
                    if radarr_cache is None:
                        radarr_cache = build_radarr_cache(_build_radarr(conn, config.secret_key))
                    caches = {**radarr_cache, **build_sonarr_cache(None)}
                else:
                    if sonarr_cache is None:
                        sonarr_cache = build_sonarr_cache(_build_sonarr(conn, config.secret_key))
                    caches = {**build_radarr_cache(None), **sonarr_cache}
                state = compute_download_state(item["media_type"], item["tmdb_id"], caches)
                if state is not None:
                    item["download_state"] = state

            all_recs[item["id"]] = item

    all_recommendations_json = json.dumps(all_recs, default=str).replace("</", "<\\/")

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "recommended.html", {
        "username": username,
        "nav_active": "recommended",
        "batches": formatted_batches,
        "enabled": enabled,
        "all_recommendations_json": all_recommendations_json,
    })


@router.get("/api/recommended")
def api_recommended(admin: str = Depends(get_current_admin)):
    """Return cached recommendations as JSON."""
    conn = get_db()
    return JSONResponse({"recommendations": _fetch_recommendations(conn)})


import threading

_refresh_lock = threading.Lock()
_refresh_running = False
_refresh_result: dict | None = None


@router.post("/api/recommended/refresh")
def api_refresh_recommendations(request: Request, admin: str = Depends(get_current_admin)):
    """Start recommendation refresh in background. Returns immediately."""
    global _refresh_running, _refresh_result
    with _refresh_lock:
        if _refresh_running:
            return JSONResponse({"status": "already_running"})
        _refresh_running = True

    from mediaman.web.routes.settings_routes import _build_plex_client

    conn = get_db()
    config = request.app.state.config
    plex = _build_plex_client(conn, config.secret_key)
    if not plex:
        with _refresh_lock:
            _refresh_running = False
        return JSONResponse({"ok": False, "error": "Plex not configured"})

    _secret_key = config.secret_key

    def run():
        global _refresh_running, _refresh_result
        result: dict
        try:
            from mediaman.db import get_db as get_db_
            from mediaman.services.openai_recommendations import refresh_recommendations
            from mediaman.web.routes.settings_routes import _build_plex_client as build_plex

            db = get_db_()
            plex_client = build_plex(db, _secret_key)
            if plex_client:
                count = refresh_recommendations(db, plex_client, manual=True)
                result = {"ok": True, "count": count}
            else:
                result = {"ok": False, "error": "Plex not configured"}
        except Exception:
            logger.exception("Background recommendation refresh failed")
            result = {"ok": False, "error": "Recommendation refresh failed"}
        with _refresh_lock:
            _refresh_result = result
            _refresh_running = False

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return JSONResponse({"status": "started"})


@router.get("/api/recommended/refresh/status")
def api_refresh_status(admin: str = Depends(get_current_admin)):
    """Poll whether the background refresh is still running."""
    with _refresh_lock:
        running = _refresh_running
        result = _refresh_result
    if running:
        return JSONResponse({"status": "running"})
    if result is not None:
        return JSONResponse({"status": "done", "result": result})
    return JSONResponse({"status": "idle"})


@router.post("/api/recommended/{recommendation_id}/download")
def api_download_recommendation(recommendation_id: int, request: Request, admin: str = Depends(get_current_admin)):
    """Add a recommended movie/show to Radarr or Sonarr and trigger download."""
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
            from mediaman.crypto import decrypt_value
            radarr_url_row = conn.execute("SELECT value FROM settings WHERE key='radarr_url'").fetchone()
            radarr_key_row = conn.execute("SELECT value, encrypted FROM settings WHERE key='radarr_api_key'").fetchone()
            if not radarr_url_row or not radarr_key_row:
                return JSONResponse({"ok": False, "error": "Radarr not configured"})
            radarr_key = radarr_key_row["value"]
            if radarr_key_row["encrypted"]:
                radarr_key = decrypt_value(radarr_key, config.secret_key, conn=conn)

            from mediaman.services.radarr import RadarrClient
            client = RadarrClient(radarr_url_row["value"], radarr_key)
            client.add_movie(tmdb_id, row["title"])
            logger.info("Added movie '%s' (tmdb:%d) to Radarr", row["title"], tmdb_id)
            from datetime import datetime, timezone
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
            conn.execute(
                "INSERT INTO download_notifications (email, title, media_type, tmdb_id, service, notified, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (notify_email, row["title"], "movie", tmdb_id, "radarr", now),
            )
            conn.commit()
            return JSONResponse({"ok": True, "message": f"Added '{row['title']}' to Radarr"})

        else:  # TV
            from mediaman.crypto import decrypt_value
            sonarr_url_row = conn.execute("SELECT value FROM settings WHERE key='sonarr_url'").fetchone()
            sonarr_key_row = conn.execute("SELECT value, encrypted FROM settings WHERE key='sonarr_api_key'").fetchone()
            if not sonarr_url_row or not sonarr_key_row:
                return JSONResponse({"ok": False, "error": "Sonarr not configured"})
            sonarr_key = sonarr_key_row["value"]
            if sonarr_key_row["encrypted"]:
                sonarr_key = decrypt_value(sonarr_key, config.secret_key, conn=conn)

            from mediaman.services.sonarr import SonarrClient
            client = SonarrClient(sonarr_url_row["value"], sonarr_key)
            # Sonarr lookup by TMDB ID to get TVDB ID
            results = client._get(f"/api/v3/series/lookup?term=tmdb:{tmdb_id}")
            if not results:
                return JSONResponse({"ok": False, "error": "Show not found in Sonarr lookup"})
            tvdb_id = results[0].get("tvdbId")
            if not tvdb_id:
                return JSONResponse({"ok": False, "error": "No TVDB ID found for this show"})

            client.add_series(tvdb_id, row["title"])
            logger.info("Added series '%s' (tvdb:%d) to Sonarr", row["title"], tvdb_id)
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE suggestions SET downloaded_at = ? WHERE id = ?",
                (now, recommendation_id),
            )
            admin_row = conn.execute(
                "SELECT email FROM subscribers WHERE active=1 LIMIT 1"
            ).fetchone()
            notify_email = admin_row["email"] if admin_row else admin
            conn.execute(
                "INSERT INTO download_notifications (email, title, media_type, tmdb_id, service, notified, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (notify_email, row["title"], "tv", tmdb_id, "sonarr", now),
            )
            conn.commit()
            return JSONResponse({"ok": True, "message": f"Added '{row['title']}' to Sonarr"})

    except Exception as exc:
        error_msg = str(exc)
        if "already" in error_msg.lower() or "exists" in error_msg.lower():
            return JSONResponse({"ok": False, "error": f"'{row['title']}' already exists in your library"})
        logger.warning("Failed to add recommendation: %s", exc)
        return JSONResponse({"ok": False, "error": "Failed to add to download queue"})
