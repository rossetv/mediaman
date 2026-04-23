"""Dashboard page and supporting JSON API endpoints."""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.db import get_db
from mediaman.models import ACTION_SCHEDULED_DELETION
from mediaman.services.format import days_ago, format_bytes, parse_iso_utc, rk_from_audit_detail, title_from_audit_detail
from mediaman.services.storage import get_aggregate_disk_usage

logger = logging.getLogger("mediaman")

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Prefer an explicit env var; fall back to /media which is the standard
# Docker mount point.  Operators with a non-standard media root set
# MEDIAMAN_MEDIA_PATH — no code change required.
_MEDIA_PATH: str = os.environ.get("MEDIAMAN_MEDIA_PATH", "/media").strip() or "/media"


def _days_until(dt_str: str | None) -> str:
    """Return 'Deletes in N days' given an ISO datetime string, or ''."""
    execute_at = parse_iso_utc(dt_str)
    if execute_at is None:
        return ""
    delta = (execute_at - datetime.now(timezone.utc)).days
    if delta <= 0:
        return "Deletes today"
    if delta == 1:
        return "Deletes tomorrow"
    return f"Deletes in {delta} days"


def _fetch_scheduled(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return scheduled-deletion items joined with media_items, enriched for the template."""
    rows = conn.execute("""
        SELECT
            sa.id          AS sa_id,
            sa.media_item_id,
            sa.execute_at,
            mi.title,
            mi.media_type,
            mi.show_title,
            mi.season_number,
            mi.plex_rating_key,
            mi.added_at,
            mi.file_size_bytes
        FROM scheduled_actions sa
        JOIN media_items mi ON mi.id = sa.media_item_id
        WHERE sa.action = ?
          AND sa.token_used = 0
        ORDER BY sa.execute_at ASC
    """, (ACTION_SCHEDULED_DELETION,)).fetchall()

    items = []
    for r in rows:
        media_type = r["media_type"] or "movie"
        badge_class = {"movie": "badge-movie", "tv": "badge-tv", "anime": "badge-anime"}.get(
            media_type, "badge-movie"
        )
        type_label = media_type.upper()
        if media_type in ("tv", "anime") and r["season_number"]:
            type_label = f"{type_label} · S{r['season_number']}"

        items.append({
            "sa_id": r["sa_id"],
            "media_item_id": r["media_item_id"],
            "title": r["title"],
            "plex_rating_key": r["plex_rating_key"],
            "badge_class": badge_class,
            "type_label": type_label,
            "countdown": _days_until(r["execute_at"]),
            "added_ago": days_ago(r["added_at"]),
            "file_size": format_bytes(r["file_size_bytes"] or 0),
            "file_size_bytes": r["file_size_bytes"] or 0,
        })
    return items


def _fetch_recently_deleted(conn: sqlite3.Connection, secret_key: str = "") -> list[dict[str, object]]:
    """Return recent deleted audit_log entries joined with media_items."""
    rows = conn.execute("""
        SELECT
            al.id,
            al.media_item_id,
            al.created_at,
            al.detail,
            al.space_reclaimed_bytes,
            mi.title,
            mi.media_type,
            mi.plex_rating_key
        FROM audit_log al
        LEFT JOIN media_items mi ON mi.id = al.media_item_id
        WHERE al.action = 'deleted'
        ORDER BY al.created_at DESC
        LIMIT 20
    """).fetchall()

    # Fetch re-downloads with timestamps so we can compare per-deletion
    redownloaded = conn.execute(
        "SELECT media_item_id, created_at FROM audit_log "
        "WHERE action IN ('re_downloaded', 'downloaded')"
    ).fetchall()
    # Map lowercase title → latest re-download timestamp
    redownload_times: dict[str, str] = {}
    for rd in redownloaded:
        key = rd["media_item_id"].lower()
        if key not in redownload_times or rd["created_at"] > redownload_times[key]:
            redownload_times[key] = rd["created_at"]

    items = []
    titles_needing_poster = []  # (index, title) for items without a Plex poster

    for r in rows:
        title = r["title"]
        if not title:
            title = title_from_audit_detail(r["detail"])
        # Skip only if there's a re-download AFTER this specific deletion
        last_redownload = redownload_times.get(title.lower())
        if last_redownload and last_redownload > r["created_at"]:
            continue
        rk = r["plex_rating_key"] or rk_from_audit_detail(r["detail"])
        poster_url = f"/api/poster/{rk}" if rk else ""
        idx = len(items)
        items.append({
            "id": r["id"],
            "media_item_id": r["media_item_id"],
            "title": title,
            "poster_url": poster_url,
            "deleted_ago": days_ago(r["created_at"]),
            "reclaimed": format_bytes(r["space_reclaimed_bytes"] or 0),
        })
        if not poster_url:
            titles_needing_poster.append((idx, title))
        if len(items) >= 10:
            break

    # Fall back to TMDB poster for items without a Plex rating key
    if titles_needing_poster:
        _fill_tmdb_posters(conn, items, titles_needing_poster, secret_key)

    return items


def _fill_tmdb_posters(
    conn: sqlite3.Connection,
    items: list[dict[str, object]],
    needed: list[tuple[int, str]],
    secret_key: str,
) -> None:
    """Look up TMDB poster URLs for deleted items missing a Plex poster.

    Reuses the shared :class:`TmdbClient`. Deduplicates by title so
    repeated entries (e.g. multiple "Barbie" deletions) only trigger one
    API call. Poster URLs use the w200 thumbnail size for the dashboard
    tiles (callers elsewhere use w300 — kept deliberately distinct).

    ``secret_key`` is threaded in from the request handler to avoid
    redundant ``load_config()`` calls per request (H25).
    """
    from mediaman.services.tmdb import TmdbClient

    # Deleted-poster lookups run on the dashboard page load; keep the
    # original 5s timeout so a flaky TMDB doesn't slow the page down.
    client = TmdbClient.from_db(conn, secret_key, timeout=5.0)
    if client is None:
        return

    cache: dict[str, str] = {}

    for idx, title in needed:
        if title in cache:
            items[idx]["poster_url"] = cache[title]
            continue
        best = client.search_multi(title)
        if best and best.get("poster_path"):
            url = f"https://image.tmdb.org/t/p/w200{best['poster_path']}"
            items[idx]["poster_url"] = url
            cache[title] = url
            continue
        cache[title] = ""


def _fetch_storage_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return storage stats dict for the dashboard template.

    Disk usage is read from _MEDIA_PATH; falls back to zeroes gracefully.
    Per-type sizes come from summing file_size_bytes on media_items.
    """
    # Disk-level stats — aggregate across all unique mount points under /media
    try:
        disk = get_aggregate_disk_usage(_MEDIA_PATH)
        total = disk["total_bytes"]
        used = disk["used_bytes"]
        free = disk["free_bytes"]
    except Exception:
        total = used = free = 0

    # Per-type breakdown from DB
    type_rows = conn.execute("""
        SELECT media_type, SUM(file_size_bytes) AS total
        FROM media_items
        GROUP BY media_type
    """).fetchall()

    type_sizes = {r["media_type"]: (r["total"] or 0) for r in type_rows}
    movies_bytes = type_sizes.get("movie", 0)
    tv_bytes = type_sizes.get("tv_season", 0) + type_sizes.get("tv", 0) + type_sizes.get("season", 0)
    anime_bytes = type_sizes.get("anime_season", 0) + type_sizes.get("anime", 0)
    known_bytes = movies_bytes + tv_bytes + anime_bytes
    other_bytes = max(0, used - known_bytes) if used else 0

    def pct(val: int) -> float:
        return round(val / total * 100, 1) if total else 0.0

    return {
        "used": format_bytes(used),
        "total": format_bytes(total),
        "free": format_bytes(free),
        "movies_bytes": movies_bytes,
        "tv_bytes": tv_bytes,
        "anime_bytes": anime_bytes,
        "other_bytes": other_bytes,
        "movies_label": format_bytes(movies_bytes),
        "tv_label": format_bytes(tv_bytes),
        "anime_label": format_bytes(anime_bytes),
        "other_label": format_bytes(other_bytes),
        "movies_pct": pct(movies_bytes),
        "tv_pct": pct(tv_bytes),
        "anime_pct": pct(anime_bytes),
        "other_pct": pct(other_bytes),
    }


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request) -> Response:
    """Render the admin dashboard. Redirects to /login if session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    config = request.app.state.config
    scheduled_items = _fetch_scheduled(conn)
    recently_deleted = _fetch_recently_deleted(conn, config.secret_key)
    storage = _fetch_storage_stats(conn)

    # Aggregate totals for section subtitles
    scheduled_count = len(scheduled_items)
    scheduled_size = format_bytes(sum(i["file_size_bytes"] for i in scheduled_items))

    # SUM always returns a row; value is NULL when audit_log is empty.
    reclaimed_total_row = conn.execute(
        "SELECT SUM(space_reclaimed_bytes) AS total FROM audit_log WHERE action='deleted'"
    ).fetchone()
    reclaimed_total = format_bytes(reclaimed_total_row["total"] or 0)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "dashboard.html", {
        "username": username,
        "nav_active": "dashboard",
        "storage": storage,
        "scheduled_items": scheduled_items,
        "scheduled_count": scheduled_count,
        "scheduled_size": scheduled_size,
        "recently_deleted": recently_deleted,
        "reclaimed_total": reclaimed_total,
    })


# ---------------------------------------------------------------------------
# JSON API endpoints
# ---------------------------------------------------------------------------

@router.get("/api/dashboard/stats")
def api_dashboard_stats(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return storage usage and reclaimed-space totals as JSON."""
    conn = get_db()
    storage = _fetch_storage_stats(conn)

    row = conn.execute(
        "SELECT SUM(space_reclaimed_bytes) AS total FROM audit_log WHERE action='deleted'"
    ).fetchone()
    reclaimed_bytes = row["total"] or 0

    return JSONResponse({
        "storage": storage,
        "reclaimed_total_bytes": reclaimed_bytes,
        "reclaimed_total": format_bytes(reclaimed_bytes),
    })


@router.get("/api/dashboard/scheduled")
def api_dashboard_scheduled(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return scheduled-deletion items as JSON."""
    conn = get_db()
    return JSONResponse({"items": _fetch_scheduled(conn)})


@router.get("/api/dashboard/deleted")
def api_dashboard_deleted(request: Request, username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return recently deleted items from audit_log as JSON."""
    conn = get_db()
    secret_key = request.app.state.config.secret_key
    return JSONResponse({"items": _fetch_recently_deleted(conn, secret_key)})


@router.get("/api/dashboard/reclaimed-chart")
def api_dashboard_reclaimed_chart(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return weekly reclaimed-space aggregates grouped by ISO week.

    Each row: { week: 'YYYY-WNN', reclaimed_bytes: int, reclaimed: str }
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT
            strftime('%Y-W%W', created_at) AS week,
            SUM(space_reclaimed_bytes)     AS reclaimed_bytes
        FROM audit_log
        WHERE action = 'deleted'
          AND space_reclaimed_bytes IS NOT NULL
        GROUP BY week
        ORDER BY week DESC
        LIMIT 12
    """).fetchall()

    data = [
        {
            "week": r["week"],
            "reclaimed_bytes": r["reclaimed_bytes"] or 0,
            "reclaimed": format_bytes(r["reclaimed_bytes"] or 0),
        }
        for r in rows
    ]
    return JSONResponse({"weeks": data})
