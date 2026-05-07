"""Data-fetching helpers for the dashboard page."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime

from mediaman.core.format import (
    days_ago,
    format_bytes,
    media_type_badge,
    rk_from_audit_detail,
    title_from_audit_detail,
)
from mediaman.core.time import parse_iso_utc
from mediaman.services.infra.settings_reader import get_media_path as _get_media_path
from mediaman.services.infra.storage import get_aggregate_disk_usage
from mediaman.web.models import ACTION_SCHEDULED_DELETION

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bounded scan size so the dashboard render doesn't degenerate into a
# long table walk on a heavy audit_log. 5 batches × 50 rows = 250
# candidates is enough headroom for any realistic mix of re-downloaded
# and orphan-titled deletions.
_RECENT_DELETED_BATCH = 50
_RECENT_DELETED_MAX_BATCHES = 5

# Cache window for the disk-usage stat. statvfs() is cheap but a busy
# dashboard on a slow filesystem could still spend tens of milliseconds
# per render hitting it; 30s is well below the granularity at which a
# user notices stale "free space" numbers.
_DISK_USAGE_CACHE_TTL_SECONDS = 30.0
_disk_usage_cache: dict[str, tuple[float, dict[str, int]]] = {}
_disk_usage_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _days_until(dt_str: str | None) -> str:
    """Return 'Deletes in N days' given an ISO datetime string, or ''."""
    execute_at = parse_iso_utc(dt_str)
    if execute_at is None:
        return ""
    delta = (execute_at - datetime.now(UTC)).days
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
) -> dict[str, str]:
    """Return ``by_title_lower`` mapping of re-download timestamps.

    The audit_log ``media_item_id`` column carries different content
    depending on the action:

    * ``deleted`` rows: a stable UUID matching ``media_items.id``.
    * ``re_downloaded`` rows written before a fix: the free-text resolved title.
    * ``re_downloaded`` rows written after the fix: a stable
      ``tmdb:<id>`` / ``tvdb:<id>`` / ``imdb:<id>`` token.
    * ``downloaded`` rows: usually the title (legacy behaviour
      preserved by the recommendations pipeline).

    To stay correct across the migration window we build a title-keyed
    index. Title matches stay lower-cased. When the deletion row carries
    a tmdb_id we can also consult the redownload index by tmdb_id; for
    now we only match on title.
    """
    by_title_lower: dict[str, str] = {}
    rows = conn.execute(
        "SELECT media_item_id, created_at FROM audit_log "
        "WHERE action IN ('re_downloaded', 'downloaded')"
    ).fetchall()
    for rd in rows:
        raw = rd["media_item_id"] or ""
        ts = rd["created_at"]
        # Skip prefixed tokens (tmdb:/tvdb:/imdb:) — no title match possible.
        if ":" in raw:
            continue
        # Legacy / non-prefixed rows — treat the whole string as a
        # title. Lower-case so "Dune" matches "dune".
        key = raw.lower()
        if not key:
            continue
        prev_t = by_title_lower.get(key)
        if prev_t is None or ts > prev_t:
            by_title_lower[key] = ts
    return by_title_lower


def _was_redownloaded_after(
    deletion_created_at: str,
    *,
    title: str,
    by_title_lower: dict[str, str],
) -> bool:
    """Return True if a re-download for *title* happened after *deletion_created_at*.

    Looks up the title-keyed index. The index is passed explicitly so
    callers can build it once per page render rather than rebuilding it
    per deletion row.
    """
    last_redownload = by_title_lower.get(title.lower())
    return bool(last_redownload and last_redownload > deletion_created_at)


def _fetch_recently_deleted(
    conn: sqlite3.Connection, secret_key: str = ""
) -> list[dict[str, object]]:
    """Return up to 10 recent ``deleted`` audit_log entries.

    Skips deletions that have a more-recent re-download. The earlier
    implementation issued a single ``LIMIT 20`` query and post-filtered;
    when most rows had a re-download the result was short of 10 with no
    retry. Now we page through audit_log in batches until we have 10
    unfiltered items or hit a hard cap.
    """
    from mediaman.web.routes.dashboard._poster_fanout import _fill_tmdb_posters

    by_title_lower = _build_redownload_index(conn)

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
            # When the deletion row carries a tmdb_id we can also consult the redownload index
            # by tmdb_id; for now we only match on title.
            if _was_redownloaded_after(
                r["created_at"],
                title=title,
                by_title_lower=by_title_lower,
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


def _cached_disk_usage(media_path: str) -> dict[str, int]:
    """Return ``get_aggregate_disk_usage`` results, cached for 30s.

    Misses still hit the underlying call; the cache only short-circuits
    repeats within the TTL. Keyed on ``media_path`` so a settings
    change to the configured path invalidates the cache automatically.
    """
    now = time.monotonic()
    with _disk_usage_cache_lock:
        entry = _disk_usage_cache.get(media_path)
        if entry is not None and now - entry[0] < _DISK_USAGE_CACHE_TTL_SECONDS:
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
