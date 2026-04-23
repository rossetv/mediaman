"""Library page — browse, search, filter, and act on all media items."""

from __future__ import annotations

import difflib
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as _url_quote

import requests as _requests

from fastapi import APIRouter, Body, Depends, Form, Query, Request
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
    params: list[object] = []

    if q:
        where_clauses.append("(title LIKE ? ESCAPE '\\' OR show_title LIKE ? ESCAPE '\\')")
        # Escape LIKE metacharacters so a search for "50%" or "foo_bar"
        # matches literally rather than as wildcards.
        q_escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{q_escaped}%"
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
    # SQLite COUNT(*) always returns exactly one row so the `if count_row`
    # guard is dead code — simplify to a direct index access.
    count_row = conn.execute(
        cte_sql + f"SELECT COUNT(*) AS n FROM display_items{kept_where}", params,
    ).fetchone()
    total = count_row["n"]

    # ── Fetch page ───────────────────────────────────────────────────────
    offset = (page - 1) * per_page
    # Cap offset so a pathological page= parameter cannot trigger a full
    # table scan inside SQLite. 50 000 rows is a generous upper bound for
    # any real library and keeps query time bounded.
    offset = min(offset, 50_000)
    rows = conn.execute(
        cte_sql + f"SELECT * FROM display_items{kept_where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    # ── Batch-fetch protection status — eliminates N+1 ───────────────────
    # Collect IDs and show keys from the page, then do two queries that
    # cover all items at once instead of two queries per row.
    item_ids    = [r["id"] for r in rows]
    show_rkeys  = {r["show_rating_key"] for r in rows if r["show_rating_key"]}

    # Map media_item_id → (action, execute_at) for scheduled protections
    sa_map: dict[str, tuple[str, str | None]] = {}
    if item_ids:
        ph = ",".join("?" * len(item_ids))
        for sa in conn.execute(
            f"SELECT media_item_id, action, execute_at "
            f"FROM scheduled_actions "
            f"WHERE media_item_id IN ({ph}) AND token_used = 0 "
            f"AND action IN ('protected_forever', 'snoozed')",
            item_ids,
        ).fetchall():
            # Keep only the most protective entry per item; the query
            # doesn't guarantee ordering so we prefer protected_forever.
            prev = sa_map.get(sa["media_item_id"])
            if prev is None or prev[0] != ACTION_PROTECTED_FOREVER:
                sa_map[sa["media_item_id"]] = (sa["action"], sa["execute_at"])

    # Map show_rating_key → (action, execute_at) for show-level keeps
    ks_map: dict[str, tuple[str, str | None]] = {}
    if show_rkeys:
        ph = ",".join("?" * len(show_rkeys))
        for ks in conn.execute(
            f"SELECT show_rating_key, action, execute_at "
            f"FROM kept_shows WHERE show_rating_key IN ({ph})",
            list(show_rkeys),
        ).fetchall():
            ks_map[ks["show_rating_key"]] = (ks["action"], ks["execute_at"])

    # ── Build items list ─────────────────────────────────────────────────
    items = []
    for r in rows:
        display_type = r["display_type"]
        is_tv = display_type in ("tv", "anime")
        show_rk = r["show_rating_key"] or ""
        show_title = r["show_title"] or r["title"]

        # Protection: TV rows check kept_shows first; movie rows go
        # straight to scheduled_actions. All lookups now use the
        # pre-fetched maps above (no per-row queries).
        protected = False
        protection_label = None
        if is_tv and show_rk:
            ks_entry = ks_map.get(show_rk)
            if ks_entry:
                protection_label = _protection_label(ks_entry[0], ks_entry[1])
                protected = protection_label is not None
        if not protected:
            sa_entry = sa_map.get(str(r["id"]))
            if sa_entry:
                protection_label = _protection_label(sa_entry[0], sa_entry[1])
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
    # SQLite COUNT(*) always returns a row — `if count_row else 0` is dead.
    movies = conn.execute(
        "SELECT COUNT(*) AS n FROM media_items WHERE media_type = 'movie'"
    ).fetchone()["n"]

    tv = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(show_rating_key, show_title)) AS n "
        "FROM media_items WHERE media_type IN ('tv_season', 'tv', 'season')"
    ).fetchone()["n"]

    anime = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(show_rating_key, show_title)) AS n "
        "FROM media_items WHERE media_type IN ('anime_season', 'anime')"
    ).fetchone()["n"]

    # Read thresholds from settings, falling back to sensible defaults
    min_age = get_int_setting(conn, "min_age_days", default=30)
    inactivity = get_int_setting(conn, "inactivity_days", default=30)

    now = datetime.now(timezone.utc)
    age_cutoff = (now - timedelta(days=min_age)).isoformat()
    watch_cutoff = (now - timedelta(days=inactivity)).isoformat()

    stale = conn.execute("""
        SELECT COUNT(*) AS n
        FROM media_items
        WHERE added_at < ?
          AND (last_watched_at IS NULL OR last_watched_at < ?)
    """, (age_cutoff, watch_cutoff)).fetchone()["n"]

    total = movies + tv + anime
    # SUM may return NULL when media_items is empty; guard with `or 0`.
    total_size_row = conn.execute("SELECT SUM(file_size_bytes) AS n FROM media_items").fetchone()
    total_size = format_bytes(total_size_row["n"] or 0)

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
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Return paginated library items as JSON.

    Query params: q (search text), type (movie/tv/anime), sort, page, per_page.
    """
    conn = get_db()
    sort = sort if sort in _VALID_SORTS else "added_desc"
    media_type = type if type in _VALID_TYPES else ""

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

# Per-admin cap on keep/snooze actions. 60/min is generous for UI use
# but stops a scripted loop from filling scheduled_actions endlessly.
_KEEP_LIMITER = ActionRateLimiter(
    max_in_window=60, window_seconds=60, max_per_day=500,
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

    # Phase 1 — read the row under a BEGIN IMMEDIATE so a concurrent
    # delete can't race us to the Arr call. Row data is copied into
    # locals so we can release the lock before doing HTTP: Arr I/O
    # must NEVER happen while a write transaction is open (C22).
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, title, media_type, file_path, file_size_bytes, radarr_id, sonarr_id, season_number, plex_rating_key "
            "FROM media_items WHERE id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "Not found"}, status_code=404)
        snapshot = {
            "title": row["title"],
            "media_type": row["media_type"],
            "file_path": row["file_path"],
            "file_size_bytes": row["file_size_bytes"],
            "radarr_id": row["radarr_id"],
            "sonarr_id": row["sonarr_id"],
            "season_number": row["season_number"],
            "plex_rating_key": row["plex_rating_key"],
        }
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    title = snapshot["title"]
    config = request.app.state.config
    is_movie = snapshot["media_type"] == "movie"

    # Phase 2 — outside the DB transaction, ask Arr to delete. Any HTTP
    # error (except 404, which we treat as "already gone") becomes a
    # 502 and the DB row is left intact so the caller can retry
    # idempotently once Arr is reachable again.
    def _is_already_gone(exc: Exception) -> bool:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
        return status == 404

    if is_movie:
        client = build_radarr_from_db(conn, config.secret_key)
        if client:
            radarr_id = snapshot["radarr_id"]
            if radarr_id:
                try:
                    client.delete_movie(radarr_id)
                    logger.info("Deleted '%s' via Radarr (id %s, with files + exclusion)", title, radarr_id)
                except Exception as exc:
                    if _is_already_gone(exc):
                        logger.info("Radarr reports id %s already gone for '%s' — idempotent delete", radarr_id, title)
                    else:
                        logger.warning("Radarr delete failed for '%s': %s", title, exc, exc_info=True)
                        return JSONResponse(
                            {"ok": False, "error": "Upstream Radarr delete failed — DB row preserved"},
                            status_code=502,
                        )
            else:
                logger.info(
                    "No stored radarr_id for '%s' — skipping Radarr-level delete. "
                    "Run a full scan to populate radarr_id if you need file deletion.",
                    title,
                )
    else:
        client = build_sonarr_from_db(conn, config.secret_key)
        if client:
            sid = snapshot["sonarr_id"]
            season_num = snapshot["season_number"]
            if sid and season_num is not None:
                try:
                    client.delete_episode_files(sid, season_num)
                    client.unmonitor_season(sid, season_num)
                    logger.info("Deleted season files for '%s' S%s via Sonarr", title, season_num)
                    if not client.has_remaining_files(sid):
                        client.delete_series(sid)
                        logger.info("No files remain for '%s' — deleted series from Sonarr with exclusion", title)
                except Exception as exc:
                    if _is_already_gone(exc):
                        logger.info("Sonarr reports id %s already gone for '%s' — idempotent delete", sid, title)
                    else:
                        logger.warning("Sonarr delete failed for '%s': %s", title, exc, exc_info=True)
                        return JSONResponse(
                            {"ok": False, "error": "Upstream Sonarr delete failed — DB row preserved"},
                            status_code=502,
                        )

    # Phase 3 — Arr confirmed (or not configured). Reopen a transaction
    # and prune the DB rows.
    rk = snapshot["plex_rating_key"] or ""
    detail = f"Deleted '{title}' by {username}"
    if rk:
        detail += f" [rk:{rk}]"
    try:
        conn.execute("BEGIN IMMEDIATE")
        log_audit(conn, media_id, "deleted", detail, space_bytes=snapshot["file_size_bytes"])
        conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_id,))
        conn.execute("DELETE FROM media_items WHERE id = ?", (media_id,))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    logger.info("Deleted %s (%s) — %s by %s", media_id, title, snapshot["file_path"], username)
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
    if not _KEEP_LIMITER.check(username):
        logger.warning("media.keep_throttled user=%s", username)
        return JSONResponse(
            {"error": "Too many keep operations — slow down"},
            status_code=429,
        )

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
        execute_at = (now + timedelta(days=int(days))).isoformat()
        snooze_label = duration

    # Use BEGIN IMMEDIATE so a concurrent keep-request for the same
    # media_item_id can't race between the SELECT and the INSERT/UPDATE.
    # scheduled_actions has no UNIQUE constraint on media_item_id (multiple
    # rows per item are valid in the general schema), so ON CONFLICT is not
    # available — we guard the critical section with a write lock instead.
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id = ? AND token_used = 0",
            (media_id,),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE scheduled_actions
                   SET action=?, execute_at=?, snoozed_at=?, snooze_duration=?, token_used=0
                   WHERE id=?""",
                (action, execute_at, now.isoformat(), snooze_label, existing["id"]),
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
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # Audit
    log_audit(conn, media_id, "snoozed", f"Kept for {snooze_label} by admin ({username})")

    conn.commit()
    logger.info("Media item %s protected for %s by %s", media_id, snooze_label, username)

    return JSONResponse({"ok": True, "id": media_id, "duration": snooze_label})


# Minimum title similarity accepted for a title+year fuzzy match.
# Pairs well below this (e.g. "Inception 2010" vs "Inception 2020") will
# not clear the bar, so the picker will refuse rather than add the
# wrong entry.
_REDOWNLOAD_TITLE_SIMILARITY = 0.9


def _pick_lookup_match(
    lookup: list[dict],
    *,
    title: str,
    year: int | None,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
    id_keys: tuple[str, ...],
) -> tuple[dict | None, str | None]:
    """Return ``(entry, error)`` for a Radarr/Sonarr lookup response.

    IDs win — if any provided ID matches exactly one row, that row is
    used. Otherwise fall back to a fuzzy title+year match with a tight
    similarity threshold and a hard year equality requirement. Returns
    ``(None, reason)`` if no acceptable match is found or the result
    is ambiguous.

    ``id_keys`` is the list of ID field names to check on lookup rows
    (e.g. ``("tmdbId",)`` for Radarr movies, ``("tvdbId", "tmdbId",
    "imdbId")`` for Sonarr series).
    """
    if not lookup:
        return None, "No lookup results"

    # ID path — strongest signal, zero ambiguity tolerated.
    wanted_ids: dict[str, object] = {}
    if tmdb_id is not None:
        wanted_ids["tmdbId"] = tmdb_id
    if tvdb_id is not None:
        wanted_ids["tvdbId"] = tvdb_id
    if imdb_id:
        wanted_ids["imdbId"] = imdb_id

    if wanted_ids:
        hits = []
        for entry in lookup:
            for key, wanted in wanted_ids.items():
                got = entry.get(key)
                if got is None or wanted is None:
                    continue
                # Normalise both sides to strings — TMDB ids are ints,
                # IMDB ids are strings like "tt1234567".
                if str(got).strip().lower() == str(wanted).strip().lower():
                    hits.append(entry)
                    break
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, "Ambiguous ID match"
        return None, "Supplied ID did not match any lookup result"

    # Title+year path — refuse unless the top pair is similar enough
    # and the years match exactly.
    if not title:
        return None, "No title for fuzzy match"

    def _norm(s: str) -> str:
        return s.strip().lower()

    target = _norm(title)
    scored: list[tuple[float, dict]] = []
    for entry in lookup:
        cand_title = _norm(entry.get("title") or "")
        if not cand_title:
            continue
        ratio = difflib.SequenceMatcher(None, target, cand_title).ratio()
        scored.append((ratio, entry))
    if not scored:
        return None, "No titled lookup results"
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best = scored[0]
    if best_score < _REDOWNLOAD_TITLE_SIMILARITY:
        return None, "No confident title match"
    if year is None or best.get("year") != year:
        return None, "Year mismatch or missing"
    # Refuse if more than one candidate is equally close — an ambiguous
    # title+year pair must not be silently resolved.
    close = [entry for score, entry in scored if score >= _REDOWNLOAD_TITLE_SIMILARITY
             and entry.get("year") == year]
    if len(close) > 1:
        return None, "Ambiguous title+year match"
    return best, None


@router.post("/api/media/redownload")
def api_media_redownload(
    request: Request,
    body: dict = Body(...),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Re-download a deleted media item.

    Contract: the caller MUST supply at least one of ``tmdb_id``,
    ``tvdb_id``, or ``imdb_id``. ``title`` and ``year`` are accepted as
    soft hints but are never sufficient on their own — the old
    title-only branch was a delete-wrong-media vector (see C15).

    If only a title+year pair is supplied, the endpoint accepts it only
    when the Radarr/Sonarr lookup returns exactly one entry with
    ``SequenceMatcher`` ratio >= 0.9 AND an exactly-matching year.
    """
    title = str(body.get("title") or "").strip()[:256]
    year_raw = body.get("year")
    try:
        year = int(year_raw) if year_raw not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    tmdb_id = body.get("tmdb_id")
    tvdb_id = body.get("tvdb_id")
    imdb_id = body.get("imdb_id")
    try:
        tmdb_id = int(tmdb_id) if tmdb_id not in (None, "") else None
    except (TypeError, ValueError):
        tmdb_id = None
    try:
        tvdb_id = int(tvdb_id) if tvdb_id not in (None, "") else None
    except (TypeError, ValueError):
        tvdb_id = None
    if imdb_id is not None:
        imdb_id = str(imdb_id).strip() or None

    # Contract check — an ID is required unless the caller opts into a
    # tightly-constrained title+year fuzzy match. Pure title submissions
    # used to accept ``lookup[0]`` blindly which is how the wrong movie
    # could land in Radarr.
    if tmdb_id is None and tvdb_id is None and not imdb_id:
        if not title or year is None:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "Provide at least one of tmdb_id, tvdb_id, imdb_id; "
                        "title+year alone is only accepted with an exact "
                        "year and a confident title match"
                    ),
                },
                status_code=400,
            )

    if not title:
        return JSONResponse({"ok": False, "error": "No title provided"}, status_code=400)

    conn = get_db()
    config = request.app.state.config

    # Try Radarr first (movies)
    try:
        client = build_radarr_from_db(conn, config.secret_key)
        if client:
            lookup = client.lookup_by_term(_url_quote(title), endpoint="/api/v3/movie/lookup")
            entry, _err = _pick_lookup_match(
                lookup or [],
                title=title,
                year=year,
                tmdb_id=tmdb_id,
                tvdb_id=None,
                imdb_id=imdb_id,
                id_keys=("tmdbId", "imdbId"),
            )
            if entry is not None:
                resolved_tmdb = entry.get("tmdbId")
                if resolved_tmdb:
                    resolved_title = entry.get("title") or title
                    client.add_movie(resolved_tmdb, resolved_title)
                    log_audit(conn, resolved_title, "re_downloaded", f"Re-downloaded by {username}")
                    record_download_notification(
                        conn, email=username, title=resolved_title,
                        media_type="movie", tmdb_id=resolved_tmdb, service="radarr",
                    )
                    conn.commit()
                    logger.info("Re-downloaded '%s' (tmdb=%s) via Radarr by %s", resolved_title, resolved_tmdb, username)
                    return JSONResponse({"ok": True, "message": f"Added '{resolved_title}' to Radarr"})
    except _requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status in (409, 422):
            return JSONResponse({"ok": False, "error": f"'{title}' already exists in Radarr"})
        # Fall through to try Sonarr

    # Try Sonarr (TV)
    try:
        client = build_sonarr_from_db(conn, config.secret_key)
        if client:
            results = client.lookup_by_term(_url_quote(title), endpoint="/api/v3/series/lookup")
            entry, err = _pick_lookup_match(
                results or [],
                title=title,
                year=year,
                tmdb_id=tmdb_id,
                tvdb_id=tvdb_id,
                imdb_id=imdb_id,
                id_keys=("tvdbId", "tmdbId", "imdbId"),
            )
            if entry is not None:
                resolved_tvdb = entry.get("tvdbId")
                if resolved_tvdb:
                    resolved_title = entry.get("title") or title
                    client.add_series(resolved_tvdb, resolved_title)
                    resolved_tmdb_sonarr = entry.get("tmdbId")
                    log_audit(conn, resolved_title, "re_downloaded", f"Re-downloaded by {username}")
                    record_download_notification(
                        conn, email=username, title=resolved_title,
                        media_type="tv", tmdb_id=resolved_tmdb_sonarr,
                        tvdb_id=resolved_tvdb, service="sonarr",
                    )
                    conn.commit()
                    logger.info("Re-downloaded '%s' (tvdb=%s) via Sonarr by %s", resolved_title, resolved_tvdb, username)
                    return JSONResponse({"ok": True, "message": f"Added '{resolved_title}' to Sonarr"})
            # Ambiguous result — refuse with 409 so the caller surfaces
            # the conflict to the user rather than silently adding nothing.
            if err in ("Ambiguous ID match", "Ambiguous title+year match"):
                return JSONResponse(
                    {"ok": False, "error": f"Ambiguous match for '{title}' — supply tmdb_id/tvdb_id/imdb_id"},
                    status_code=409,
                )
    except _requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status in (409, 422):
            return JSONResponse({"ok": False, "error": f"'{title}' already exists in Sonarr"})
        logger.warning("Re-download via Sonarr failed for '%s': HTTP %s", title, status, exc_info=True)
        return JSONResponse({"ok": False, "error": "Download request failed — check service connectivity"})
    except Exception as exc:
        logger.warning("Re-download via Sonarr failed for '%s': %s", title, exc, exc_info=True)
        return JSONResponse({"ok": False, "error": "Download request failed — check service connectivity"})

    return JSONResponse({"ok": False, "error": f"'{title}' not found in Radarr or Sonarr"})
