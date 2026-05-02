"""Dashboard page and supporting JSON API endpoints."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.middleware import get_current_admin, resolve_page_session
from mediaman.db import get_db
from mediaman.services.infra.format import (
    days_ago,
    format_bytes,
    media_type_badge,
    parse_iso_utc,
    rk_from_audit_detail,
    title_from_audit_detail,
)
from mediaman.services.infra.settings_reader import get_media_path as _get_media_path
from mediaman.services.infra.storage import get_aggregate_disk_usage
from mediaman.web.models import ACTION_SCHEDULED_DELETION

logger = logging.getLogger("mediaman")

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: TMDB poster image base URL — w200 thumbnail size used for dashboard tiles.
#: Callers elsewhere that use w300 keep their own URL deliberately distinct.
_TMDB_POSTER_BASE_URL = "https://image.tmdb.org/t/p/w200"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    rows = conn.execute(
        """
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
    """,
        (ACTION_SCHEDULED_DELETION,),
    ).fetchall()

    items = []
    for r in rows:
        media_type = r["media_type"] or "movie"
        badge_class, type_label = media_type_badge(media_type)
        if media_type in ("tv", "anime") and r["season_number"]:
            type_label = f"{type_label} · S{r['season_number']}"

        items.append(
            {
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
            }
        )
    return items


def _build_redownload_index(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], dict[int, str]]:
    """Return ``(by_title_lower, by_tmdb_id)`` of re-download timestamps.

    The audit_log ``media_item_id`` column carries different content
    depending on the action (finding 18):

    * ``deleted`` rows: a stable UUID matching ``media_items.id``.
    * ``re_downloaded`` rows written before finding 10's fix: the
      free-text resolved title.
    * ``re_downloaded`` rows written after finding 10's fix: a stable
      ``tmdb:<id>`` / ``tvdb:<id>`` / ``imdb:<id>`` token.
    * ``downloaded`` rows: usually the title (legacy behaviour
      preserved by the recommendations pipeline).

    To stay correct across the migration window we build two indexes
    and let callers consult whichever lines up with the data they
    have. Title matches stay lower-cased; tmdb_id keys are integers
    parsed out of the ``tmdb:`` prefix.
    """
    by_title_lower: dict[str, str] = {}
    by_tmdb_id: dict[int, str] = {}
    rows = conn.execute(
        "SELECT media_item_id, created_at FROM audit_log "
        "WHERE action IN ('re_downloaded', 'downloaded')"
    ).fetchall()
    for rd in rows:
        raw = rd["media_item_id"] or ""
        ts = rd["created_at"]
        if raw.startswith("tmdb:"):
            try:
                tmdb_id = int(raw.split(":", 1)[1])
            except ValueError:
                continue
            prev = by_tmdb_id.get(tmdb_id)
            if prev is None or ts > prev:
                by_tmdb_id[tmdb_id] = ts
            continue
        # Legacy / non-prefixed rows — treat the whole string as a
        # title. Lower-case so "Dune" matches "dune".
        key = raw.lower()
        if not key:
            continue
        prev_t = by_title_lower.get(key)
        if prev_t is None or ts > prev_t:
            by_title_lower[key] = ts
    return by_title_lower, by_tmdb_id


def _was_redownloaded_after(
    deletion_created_at: str,
    *,
    title: str,
    by_title_lower: dict[str, str],
    by_tmdb_id: dict[int, str],
) -> bool:
    """Return True if a re-download for *title* happened after *deletion_created_at*.

    Looks up the title-keyed index; the tmdb-keyed lookup is reserved
    for a future schema addition that surfaces tmdb_id on the deletion
    row. The two indexes are passed explicitly so callers can build
    them once per page render rather than rebuilding them per deletion
    row.
    """
    last_redownload = by_title_lower.get(title.lower())
    if last_redownload and last_redownload > deletion_created_at:
        return True
    # Future: when the deletion row carries a tmdb_id, consult by_tmdb_id.
    _ = by_tmdb_id  # currently unused; kept for the migration target.
    return False


# Bounded scan size so the dashboard render doesn't degenerate into a
# long table walk on a heavy audit_log (finding 20). 5 batches × 50
# rows = 250 candidates is enough headroom for any realistic mix of
# re-downloaded and orphan-titled deletions.
_RECENT_DELETED_BATCH = 50
_RECENT_DELETED_MAX_BATCHES = 5


def _fetch_recently_deleted(
    conn: sqlite3.Connection, secret_key: str = ""
) -> list[dict[str, object]]:
    """Return up to 10 recent ``deleted`` audit_log entries.

    Skips deletions that have a more-recent re-download. The earlier
    implementation issued a single ``LIMIT 20`` query and post-filtered;
    when most rows had a re-download the result was short of 10 with
    no retry (finding 20). Now we page through audit_log in batches
    until we have 10 unfiltered items or hit a hard cap.
    """
    by_title_lower, by_tmdb_id = _build_redownload_index(conn)

    items: list[dict[str, object]] = []
    titles_needing_poster: list[tuple[int, str]] = []
    seen_ids: set[int] = set()

    for batch in range(_RECENT_DELETED_MAX_BATCHES):
        offset = batch * _RECENT_DELETED_BATCH
        rows = conn.execute(
            """
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
            LIMIT ? OFFSET ?
            """,
            (_RECENT_DELETED_BATCH, offset),
        ).fetchall()

        if not rows:
            break

        for r in rows:
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])

            title = r["title"]
            if not title:
                title = title_from_audit_detail(r["detail"])
            if _was_redownloaded_after(
                r["created_at"],
                title=title,
                by_title_lower=by_title_lower,
                by_tmdb_id=by_tmdb_id,
            ):
                continue

            rk = r["plex_rating_key"] or rk_from_audit_detail(r["detail"])
            poster_url = f"/api/poster/{rk}" if rk else ""
            idx = len(items)
            items.append(
                {
                    "id": r["id"],
                    "media_item_id": r["media_item_id"],
                    "title": title,
                    "media_type": r["media_type"] or "",
                    "poster_url": poster_url,
                    "deleted_ago": days_ago(r["created_at"]),
                    "reclaimed": format_bytes(r["space_reclaimed_bytes"] or 0),
                }
            )
            if not poster_url:
                titles_needing_poster.append((idx, title))
            if len(items) >= 10:
                break

        if len(items) >= 10:
            break

    # Fall back to TMDB poster for items without a Plex rating key
    if titles_needing_poster:
        _fill_tmdb_posters(conn, items, titles_needing_poster, secret_key)

    return items


# Outer wall-clock budget for the parallel poster fan-out (finding 19).
# 5s timeout × 10 misses serially produced a 50s page render in the
# worst case. Now we fan out to a small thread pool and bound the
# whole batch to 6s; anything slower drops to "" so the page still
# renders promptly.
_POSTER_FANOUT_BUDGET_SECONDS = 6.0
_POSTER_FANOUT_WORKERS = 4


@lru_cache(maxsize=1)
def _get_poster_executor() -> ThreadPoolExecutor:
    """Return the shared poster-fanout executor (lazy)."""
    return ThreadPoolExecutor(
        max_workers=_POSTER_FANOUT_WORKERS,
        thread_name_prefix="dashboard_poster",
    )


def _fill_tmdb_posters(
    conn: sqlite3.Connection,
    items: list[dict[str, object]],
    needed: list[tuple[int, str]],
    secret_key: str,
) -> None:
    """Look up TMDB poster URLs for deleted items missing a Plex poster.

    Parallelised across a small worker pool with an outer wall-clock
    budget (finding 19) — the previous sequential implementation could
    sit on a flaky TMDB for 5s × 10 misses = 50s before the page
    rendered. Deduplication by title is preserved so repeated entries
    (e.g. multiple "Barbie" deletions) only trigger one API call.

    ``secret_key`` is threaded in from the request handler to avoid
    redundant ``load_config()`` calls per request (H25).
    """
    from mediaman.services.media_meta.tmdb import TmdbClient

    client = TmdbClient.from_db(conn, secret_key, timeout=5.0)
    if client is None:
        return

    # Collect each unique title once; preserve the (idx, title)
    # reverse mapping so we can write the result back to the right
    # rows after the futures resolve.
    unique_titles: dict[str, list[int]] = {}
    for idx, title in needed:
        unique_titles.setdefault(title, []).append(idx)

    if not unique_titles:
        return

    def _lookup(title: str) -> tuple[str, str]:
        try:
            best = client.search_multi(title)
        except Exception:
            logger.debug("dashboard.poster_lookup_failed title=%r", title, exc_info=True)
            return title, ""
        if best and best.get("poster_path"):
            return title, f"{_TMDB_POSTER_BASE_URL}{best['poster_path']}"
        return title, ""

    pool = _get_poster_executor()
    futures = {pool.submit(_lookup, title): title for title in unique_titles}
    try:
        for fut in as_completed(futures, timeout=_POSTER_FANOUT_BUDGET_SECONDS):
            try:
                title, url = fut.result()
            except Exception:
                continue
            if not url:
                continue
            for idx in unique_titles.get(title, []):
                items[idx]["poster_url"] = url
    except TimeoutError:
        logger.debug(
            "dashboard.poster_fanout_timeout budget=%.1fs (%d/%d done)",
            _POSTER_FANOUT_BUDGET_SECONDS,
            sum(1 for f in futures if f.done()),
            len(futures),
        )


# Cache window for the disk-usage stat (finding 21). statvfs() is cheap
# but a busy dashboard on a slow filesystem could still spend tens of
# milliseconds per render hitting it; 30s is well below the granularity
# at which a user notices stale "free space" numbers.
_DISK_USAGE_CACHE_TTL = 30.0
_disk_usage_cache: dict[str, tuple[float, dict[str, int]]] = {}
_disk_usage_cache_lock = threading.Lock()


def _cached_disk_usage(media_path: str) -> dict[str, int]:
    """Return ``get_aggregate_disk_usage`` results, cached for 30s.

    Misses still hit the underlying call; the cache only short-circuits
    repeats within the TTL. Keyed on ``media_path`` so a settings
    change to the configured path invalidates the cache automatically.
    """
    now = time.monotonic()
    with _disk_usage_cache_lock:
        entry = _disk_usage_cache.get(media_path)
        if entry is not None and now - entry[0] < _DISK_USAGE_CACHE_TTL:
            return entry[1]
    fresh = get_aggregate_disk_usage(media_path)
    with _disk_usage_cache_lock:
        _disk_usage_cache[media_path] = (now, fresh)
    return fresh


def _fetch_storage_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return storage stats dict for the dashboard template.

    Disk usage is read from the configured media path; falls back to zeroes gracefully.
    Per-type sizes come from summing file_size_bytes on media_items.
    """
    # Disk-level stats — aggregate across all unique mount points under the media root
    try:
        disk = _cached_disk_usage(_get_media_path())
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
    tv_bytes = (
        type_sizes.get("tv_season", 0) + type_sizes.get("tv", 0) + type_sizes.get("season", 0)
    )
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
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "username": username,
            "nav_active": "dashboard",
            "storage": storage,
            "scheduled_items": scheduled_items,
            "scheduled_count": scheduled_count,
            "scheduled_size": scheduled_size,
            "recently_deleted": recently_deleted,
            "reclaimed_total": reclaimed_total,
        },
    )


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

    return JSONResponse(
        {
            "storage": storage,
            "reclaimed_total_bytes": reclaimed_bytes,
            "reclaimed_total": format_bytes(reclaimed_bytes),
        }
    )


@router.get("/api/dashboard/scheduled")
def api_dashboard_scheduled(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return scheduled-deletion items as JSON."""
    conn = get_db()
    return JSONResponse({"items": _fetch_scheduled(conn)})


@router.get("/api/dashboard/deleted")
def api_dashboard_deleted(
    request: Request, username: str = Depends(get_current_admin)
) -> JSONResponse:
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
