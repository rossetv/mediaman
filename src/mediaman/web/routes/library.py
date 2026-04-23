"""Library page — browse, search, filter, and act on all media items."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as _url_quote

from fastapi import APIRouter, Body, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.audit import log_audit
from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.auth.rate_limit import ActionRateLimiter
from mediaman.db import get_db
from mediaman.models import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED, VALID_KEEP_DURATIONS
from mediaman.services.arr_build import (
    build_radarr_from_db,
    build_sonarr_from_db,
)
from mediaman.services.download_notifications import record_download_notification
from mediaman.services.format import days_ago, format_bytes, parse_iso_utc
from mediaman.services.settings_reader import get_int_setting

logger = logging.getLogger("mediaman")

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_SORTS = {"added_desc", "added_asc", "name_asc", "name_desc", "size_desc", "size_asc", "watched_desc", "watched_asc"}
_VALID_TYPES = {"movie", "tv", "anime", "kept", "stale"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _days_ago(dt_str: str | None) -> str:
    """Return 'N days ago' or '' given an ISO datetime string.

    Thin wrapper around :func:`mediaman.services.format.days_ago` that also
    guards against suspiciously large deltas (> 10 years) from misinterpreted
    Plex timestamps.
    """
    dt = parse_iso_utc(dt_str)
    if dt is None:
        return ""
    delta = (datetime.now(timezone.utc) - dt).days
    if delta > 3650:
        return ""
    return days_ago(dt_str)


def _type_css(media_type: str) -> str:
    """Return the CSS class for a type badge."""
    if media_type in ("tv_season", "season", "tv"):
        return "type-tv"
    if media_type in ("anime_season", "anime"):
        return "type-anime"
    return "type-mov"


def _protection_label(sa_action: str | None, sa_execute_at: str | None) -> str | None:
    """Return a human-friendly protection label, or None if not protected."""
    if sa_action is None:
        return None
    if sa_action == ACTION_PROTECTED_FOREVER:
        return "Kept forever"
    if sa_action == ACTION_SNOOZED and sa_execute_at:
        try:
            execute_at = datetime.fromisoformat(sa_execute_at)
            if execute_at.tzinfo is None:
                execute_at = execute_at.replace(tzinfo=timezone.utc)
            delta = (execute_at - datetime.now(timezone.utc)).days
            if delta <= 0:
                return None  # Expired — no longer protected
            return f"Kept for {delta} more day{'s' if delta != 1 else ''}"
        except (ValueError, TypeError):
            return None
    return None


def _fetch_library(
    conn: sqlite3.Connection,
    q: str = "",
    media_type: str = "",
    sort: str = "added_desc",
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[dict[str, object]], int]:
    """Query media_items and return (items, total_count).

    TV seasons are grouped into one row per show. Movies are individual rows.
    Protection status is checked per-item (movies) or via kept_shows (TV).
    """
    # ── Build WHERE clause for the base CTE ──────────────────────────────
    where_clauses: list[str] = []
    params: list = []

    if q:
        where_clauses.append("(title LIKE ? OR show_title LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])

    kept_filter = False
    stale_filter = False
    if media_type == "kept":
        kept_filter = True
    elif media_type == "stale":
        stale_filter = True
        # Read thresholds for stale calculation
        _min_age = get_int_setting(conn, "min_age_days", default=30)
        _inactivity = get_int_setting(conn, "inactivity_days", default=30)
        _now = datetime.now(timezone.utc)
        age_cutoff = (_now - timedelta(days=_min_age)).isoformat()
        watch_cutoff = (_now - timedelta(days=_inactivity)).isoformat()
        where_clauses.append("added_at < ?")
        params.append(age_cutoff)
        where_clauses.append("(last_watched_at IS NULL OR last_watched_at < ?)")
        params.append(watch_cutoff)
    elif media_type and media_type in _VALID_TYPES:
        _TYPE_MAP = {
            "movie": ("movie",),
            "tv": ("tv_season", "tv", "season"),
            "anime": ("anime_season", "anime"),
        }
        db_types = _TYPE_MAP.get(media_type, (media_type,))
        placeholders = ",".join("?" * len(db_types))
        where_clauses.append(f"media_type IN ({placeholders})")
        params.extend(db_types)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # ── Sort mapping for the display_items CTE ───────────────────────────
    _CTE_SORT = {
        "added_desc":   "added_at DESC",
        "added_asc":    "added_at ASC",
        "name_asc":     "title ASC COLLATE NOCASE",
        "name_desc":    "title DESC COLLATE NOCASE",
        "size_desc":    "file_size_bytes DESC",
        "size_asc":     "file_size_bytes ASC",
        "watched_desc": "COALESCE(last_watched_at, '1970-01-01') DESC",
        "watched_asc":  "COALESCE(last_watched_at, '1970-01-01') ASC",
    }
    order = _CTE_SORT.get(sort, _CTE_SORT["added_desc"])

    # ── CTE: one row per movie, one row per show (grouped) ──────────────
    cte_sql = f"""
    WITH filtered AS (
        SELECT * FROM media_items {where_sql}
    ),
    display_items AS (
        -- Movies: individual rows
        SELECT
            id, title, 'movie' AS display_type,
            plex_rating_key, added_at, file_size_bytes, last_watched_at,
            show_rating_key, show_title, NULL AS season_count,
            EXISTS(
                SELECT 1 FROM scheduled_actions sa
                WHERE sa.media_item_id = filtered.id AND sa.token_used = 0
                AND sa.action IN ('protected_forever', 'snoozed')
            ) AS is_kept
        FROM filtered WHERE media_type = 'movie'

        UNION ALL

        -- TV/Anime shows: grouped by show
        SELECT
            MIN(id) AS id,
            COALESCE(show_title, title) AS title,
            CASE WHEN MAX(media_type) LIKE 'anime%' THEN 'anime' ELSE 'tv' END AS display_type,
            MIN(plex_rating_key) AS plex_rating_key,
            MAX(added_at) AS added_at,
            SUM(file_size_bytes) AS file_size_bytes,
            MAX(last_watched_at) AS last_watched_at,
            COALESCE(show_rating_key, show_title) AS show_rating_key,
            COALESCE(show_title, title) AS show_title,
            COUNT(*) AS season_count,
            (
                EXISTS(
                    SELECT 1 FROM kept_shows ks
                    WHERE ks.show_rating_key = COALESCE(filtered.show_rating_key, filtered.show_title)
                ) OR EXISTS(
                    SELECT 1 FROM scheduled_actions sa
                    WHERE sa.media_item_id IN (
                        SELECT mi2.id FROM media_items mi2
                        WHERE COALESCE(mi2.show_rating_key, mi2.show_title) = COALESCE(filtered.show_rating_key, filtered.show_title)
                    ) AND sa.token_used = 0 AND sa.action IN ('protected_forever', 'snoozed')
                )
            ) AS is_kept
        FROM filtered
        WHERE media_type IN ('tv_season', 'anime_season', 'season', 'tv', 'anime')
        GROUP BY COALESCE(show_rating_key, show_title)
    )
    """

    kept_where = " WHERE is_kept = 1" if kept_filter else ""

    # ── Count ────────────────────────────────────────────────────────────
    count_row = conn.execute(
        cte_sql + f"SELECT COUNT(*) AS n FROM display_items{kept_where}", params,
    ).fetchone()
    total = count_row["n"] if count_row else 0

    # ── Fetch page ───────────────────────────────────────────────────────
    offset = (page - 1) * per_page
    rows = conn.execute(
        cte_sql + f"SELECT * FROM display_items{kept_where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    # ── Build items list ─────────────────────────────────────────────────
    items = []
    for r in rows:
        display_type = r["display_type"]
        is_tv = display_type in ("tv", "anime")
        show_rk = r["show_rating_key"] or ""
        show_title = r["show_title"] or r["title"]

        # Protection: check kept_shows for TV, scheduled_actions for movies
        protected = False
        protection_label = None
        if is_tv and show_rk:
            ks_row = conn.execute(
                "SELECT action, execute_at FROM kept_shows WHERE show_rating_key = ?",
                (show_rk,),
            ).fetchone()
            if ks_row:
                protection_label = _protection_label(ks_row["action"], ks_row["execute_at"])
                protected = protection_label is not None
        if not protected:
            sa_row = conn.execute(
                "SELECT action, execute_at FROM scheduled_actions "
                "WHERE media_item_id = ? AND token_used = 0 "
                "AND action IN ('protected_forever', 'snoozed') LIMIT 1",
                (r["id"],),
            ).fetchone()
            if sa_row:
                protection_label = _protection_label(sa_row["action"], sa_row["execute_at"])
                protected = protection_label is not None

        season_count = r["season_count"]
        if is_tv:
            if season_count and season_count > 1:
                type_label = f"{season_count} seasons"
            else:
                type_label = "1 season"
        else:
            type_label = "MOVIE"

        added_ago = _days_ago(r["added_at"])
        subtitle_parts = []
        if added_ago:
            prefix = "Last added" if is_tv else "Added"
            subtitle_parts.append(f"{prefix} {added_ago}")

        items.append({
            "id": r["id"],
            "title": r["title"],
            "subtitle": " · ".join(subtitle_parts),
            "media_type": display_type,
            "type_label": type_label,
            "type_css": _type_css(display_type),
            "plex_rating_key": r["plex_rating_key"],
            "added_at": r["added_at"],
            "added_ago": added_ago,
            "file_size": format_bytes(r["file_size_bytes"] or 0),
            "file_size_bytes": r["file_size_bytes"] or 0,
            "last_watched": _days_ago(r["last_watched_at"]),
            "show_rating_key": show_rk,
            "show_title_raw": show_title,
            "is_tv": is_tv,
            "protected": protected,
            "protection_label": protection_label,
        })

    return items, total


def _fetch_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return counts and stale count for the library stats bar.

    Stale = added longer ago than min_age_days AND no watch activity
    within inactivity_days. Both thresholds read from the settings table.
    """
    movies_row = conn.execute(
        "SELECT COUNT(*) AS n FROM media_items WHERE media_type = 'movie'"
    ).fetchone()
    movies = movies_row["n"] if movies_row else 0

    tv_row = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(show_rating_key, show_title)) AS n "
        "FROM media_items WHERE media_type IN ('tv_season', 'tv', 'season')"
    ).fetchone()
    tv = tv_row["n"] if tv_row else 0

    anime_row = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(show_rating_key, show_title)) AS n "
        "FROM media_items WHERE media_type IN ('anime_season', 'anime')"
    ).fetchone()
    anime = anime_row["n"] if anime_row else 0

    # Read thresholds from settings, falling back to sensible defaults
    min_age = get_int_setting(conn, "min_age_days", default=30)
    inactivity = get_int_setting(conn, "inactivity_days", default=30)

    now = datetime.now(timezone.utc)
    age_cutoff = (now - timedelta(days=min_age)).isoformat()
    watch_cutoff = (now - timedelta(days=inactivity)).isoformat()

    stale_row = conn.execute("""
        SELECT COUNT(*) AS n
        FROM media_items
        WHERE added_at < ?
          AND (last_watched_at IS NULL OR last_watched_at < ?)
    """, (age_cutoff, watch_cutoff)).fetchone()
    stale = stale_row["n"] if stale_row else 0

    total = movies + tv + anime
    total_size_row = conn.execute("SELECT SUM(file_size_bytes) AS n FROM media_items").fetchone()
    total_size = format_bytes(total_size_row["n"] or 0 if total_size_row else 0)

    return {
        "movies": movies,
        "tv": tv,
        "anime": anime,
        "stale": stale,
        "stale_min_age": min_age,
        "total": total,
        "total_size": total_size,
    }


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@router.get("/library", response_class=HTMLResponse)
def library_page(
    request: Request,
    q: str = "",
    type: str = "",
    sort: str = "added_desc",
    page: int = 1,
    per_page: int = 20,
) -> Response:
    """Render the library page. Redirects to /login if session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    # Clamp + sanitise
    sort = sort if sort in _VALID_SORTS else "added_desc"
    media_type = type if type in _VALID_TYPES else ""
    page = max(1, page)
    per_page = max(1, min(100, per_page))

    items, total = _fetch_library(conn, q=q, media_type=media_type, sort=sort, page=page, per_page=per_page)
    stats = _fetch_stats(conn)

    total_pages = max(1, (total + per_page - 1) // per_page)
    page_start = (page - 1) * per_page + 1 if total else 0
    page_end = min(page * per_page, total)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "library.html", {
        "username": username,
        "nav_active": "library",
        "items": items,
        "stats": stats,
        "q": q,
        "current_type": media_type,
        "current_sort": sort,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "page_start": page_start,
        "page_end": page_end,
    })


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/library")
def api_library(
    q: str = "",
    type: str = "",
    sort: str = "added_desc",
    page: int = 1,
    per_page: int = 20,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Return paginated library items as JSON.

    Query params: q (search text), type (movie/tv/anime), sort, page, per_page.
    """
    conn = get_db()
    sort = sort if sort in _VALID_SORTS else "added_desc"
    media_type = type if type in _VALID_TYPES else ""
    page = max(1, page)
    per_page = max(1, min(100, per_page))

    items, total = _fetch_library(conn, q=q, media_type=media_type, sort=sort, page=page, per_page=per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return JSONResponse({
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    })


# Per-admin cap on media deletes — ample for legitimate cleanup,
# stops a compromised session nuking a library in an afternoon.
_DELETE_LIMITER = ActionRateLimiter(
    max_in_window=20, window_seconds=60, max_per_day=300,
)


@router.post("/api/media/{media_id}/delete")
def api_media_delete(
    media_id: str,
    request: Request,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Delete a media item via Radarr/Sonarr (deletes files + adds to exclusion list)."""
    if not _DELETE_LIMITER.check(username):
        logger.warning("media.delete_throttled user=%s", username)
        return JSONResponse(
            {"error": "Too many delete operations — slow down"},
            status_code=429,
        )
    conn = get_db()

    row = conn.execute(
        "SELECT id, title, media_type, file_path, file_size_bytes, radarr_id, sonarr_id, season_number, plex_rating_key "
        "FROM media_items WHERE id = ?",
        (media_id,),
    ).fetchone()
    if row is None:
        return JSONResponse({"error": "Not found"}, status_code=404)

    title = row["title"]
    now = datetime.now(timezone.utc).isoformat()
    config = request.app.state.config
    is_movie = row["media_type"] == "movie"

    # Delete via Radarr (movies) — deletes files + adds import exclusion
    if is_movie:
        try:
            client = build_radarr_from_db(conn, config.secret_key)
            if client:
                # Require the radarr_id recorded at scan time. Title
                # matching is dangerous — multiple items may share a
                # title across libraries (remake/reboot/alternate cut)
                # and a compromised or misconfigured Plex library could
                # poison the stored title, causing the wrong Radarr
                # movie to be deleted with its files.
                radarr_id = row["radarr_id"]
                if radarr_id:
                    client.delete_movie(radarr_id)
                    logger.info("Deleted '%s' via Radarr (id %s, with files + exclusion)", title, radarr_id)
                else:
                    logger.info(
                        "No stored radarr_id for '%s' — skipping Radarr-level delete. "
                        "Run a full scan to populate radarr_id if you need file deletion.",
                        title,
                    )
        except Exception as exc:
            logger.warning("Radarr delete failed for '%s': %s", title, exc, exc_info=True)

    # Delete via Sonarr (TV) — delete episode files + unmonitor season
    else:
        try:
            client = build_sonarr_from_db(conn, config.secret_key)
            if client:
                # Same rule as Radarr — require stored sonarr_id; no
                # title-based lookup fallback.
                sid = row["sonarr_id"]
                season_num = row["season_number"]
                if sid and season_num is not None:
                    client.delete_episode_files(sid, season_num)
                    client.unmonitor_season(sid, season_num)
                    logger.info("Deleted season files for '%s' S%s via Sonarr", title, season_num)
                    # If no files remain for the series, remove it entirely + add exclusion
                    if not client.has_remaining_files(sid):
                        client.delete_series(sid)
                        logger.info("No files remain for '%s' — deleted series from Sonarr with exclusion", title)
        except Exception as exc:
            logger.warning("Sonarr delete failed for '%s': %s", title, exc, exc_info=True)

    # Audit log — include title and poster key so they survive media_items deletion
    rk = row["plex_rating_key"] or ""
    detail = f"Deleted '{title}' by {username}"
    if rk:
        detail += f" [rk:{rk}]"
    log_audit(conn, media_id, "deleted", detail, space_bytes=row["file_size_bytes"])

    # Remove scheduled actions and the media item itself
    conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_id,))
    conn.execute("DELETE FROM media_items WHERE id = ?", (media_id,))
    conn.commit()

    logger.info("Deleted %s (%s) — %s by %s", media_id, row["title"], row["file_path"], username)
    return JSONResponse({"ok": True, "id": media_id})


@router.post("/api/media/{media_id}/keep")
def api_media_keep(
    media_id: str,
    duration: str = Form(...),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Apply protection to a media item.

    Duration must be one of: '7 days', '30 days', '90 days', 'forever'.
    Inserts or replaces the scheduled_actions row for the item.
    """
    conn = get_db()

    if duration not in VALID_KEEP_DURATIONS:
        return JSONResponse({"error": "Invalid duration"}, status_code=400)

    # Verify item exists
    row = conn.execute("SELECT id FROM media_items WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": "Not found"}, status_code=404)

    now = datetime.now(timezone.utc)

    if duration == "forever":
        action = ACTION_PROTECTED_FOREVER
        execute_at = None
        snooze_label = "forever"
    else:
        days = VALID_KEEP_DURATIONS[duration]
        action = ACTION_SNOOZED
        execute_at = (now + timedelta(days=days)).isoformat()  # type: ignore[arg-type]
        snooze_label = duration

    # Check for an existing active scheduled action for this item
    existing = conn.execute(
        "SELECT id FROM scheduled_actions WHERE media_item_id = ? AND token_used = 0",
        (media_id,),
    ).fetchone()

    import secrets

    if existing:
        conn.execute(
            """UPDATE scheduled_actions
               SET action=?, execute_at=?, snoozed_at=?, snooze_duration=?, token_used=0
               WHERE media_item_id = ? AND token_used = 0""",
            (action, execute_at, now.isoformat(), snooze_label, media_id),
        )
    else:
        conn.execute(
            """INSERT INTO scheduled_actions
               (media_item_id, action, scheduled_at, execute_at, token, token_used,
                snoozed_at, snooze_duration)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
            (media_id, action, now.isoformat(), execute_at,
             secrets.token_urlsafe(32), now.isoformat(), snooze_label),
        )

    # Audit
    log_audit(conn, media_id, "snoozed", f"Kept for {snooze_label} by admin ({username})")

    conn.commit()
    logger.info("Media item %s protected for %s by %s", media_id, snooze_label, username)

    return JSONResponse({"ok": True, "id": media_id, "duration": snooze_label})


@router.post("/api/media/redownload")
def api_media_redownload(
    request: Request,
    title: str = Body(..., embed=True),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Re-download a deleted media item by searching Radarr/Sonarr by title."""
    title = title.strip()
    if not title:
        return JSONResponse({"ok": False, "error": "No title provided"}, status_code=400)

    conn = get_db()
    config = request.app.state.config
    now = datetime.now(timezone.utc).isoformat()

    # Try Radarr first (movies)
    try:
        client = build_radarr_from_db(conn, config.secret_key)
        if client:
            lookup = client._get(f"/api/v3/movie/lookup?term={_url_quote(title)}")
            if lookup:
                tmdb_id = lookup[0].get("tmdbId")
                if tmdb_id:
                    client.add_movie(tmdb_id, title)
                    log_audit(conn, title, "re_downloaded", f"Re-downloaded by {username}")
                    record_download_notification(conn, email=username, title=title, media_type="movie", tmdb_id=tmdb_id, service="radarr")
                    conn.commit()
                    logger.info("Re-downloaded '%s' via Radarr by %s", title, username)
                    return JSONResponse({"ok": True, "message": f"Added '{title}' to Radarr"})
    except Exception as exc:
        error_msg = str(exc)
        if "already" in error_msg.lower() or "exists" in error_msg.lower():
            return JSONResponse({"ok": False, "error": f"'{title}' already exists in Radarr"})
        # Fall through to try Sonarr

    # Try Sonarr (TV)
    try:
        client = build_sonarr_from_db(conn, config.secret_key)
        if client:
            results = client._get(f"/api/v3/series/lookup?term={_url_quote(title)}")
            if results:
                tvdb_id = results[0].get("tvdbId")
                if tvdb_id:
                    client.add_series(tvdb_id, title)
                    tmdb_id_sonarr = results[0].get("tmdbId")
                    log_audit(conn, title, "re_downloaded", f"Re-downloaded by {username}")
                    # Sonarr matches series by TVDB id — keep both IDs so
                    # the completion checker can fire even when tmdbId
                    # isn't populated on the series record.
                    record_download_notification(conn, email=username, title=title, media_type="tv", tmdb_id=tmdb_id_sonarr, tvdb_id=tvdb_id, service="sonarr")
                    conn.commit()
                    logger.info("Re-downloaded '%s' via Sonarr by %s", title, username)
                    return JSONResponse({"ok": True, "message": f"Added '{title}' to Sonarr"})
    except Exception as exc:
        error_msg = str(exc)
        if "already" in error_msg.lower() or "exists" in error_msg.lower():
            return JSONResponse({"ok": False, "error": f"'{title}' already exists in Sonarr"})
        logger.warning("Re-download via Sonarr failed for '%s': %s", title, exc, exc_info=True)
        return JSONResponse({"ok": False, "error": "Download request failed — check service connectivity"})

    return JSONResponse({"ok": False, "error": f"'{title}' not found in Radarr or Sonarr"})
