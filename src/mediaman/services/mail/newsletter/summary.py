"""Disk-usage aggregation, reclaimed-space totals, recently-deleted cards."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from mediaman.crypto import sign_poster_url
from mediaman.services.infra.format import rk_from_audit_detail as _extract_rk_from_detail
from mediaman.services.infra.format import title_from_audit_detail as _extract_title_from_detail

from ._time import _parse_days_ago

logger = logging.getLogger("mediaman")


def _load_deleted_items(
    conn: sqlite3.Connection,
    secret_key: str,
    base_url: str,
    now: datetime,
) -> list[dict]:
    """Query and build the recently-deleted card list (last 7 days).

    Items that were re-downloaded after their deletion timestamp are silently
    excluded so the newsletter doesn't ask subscribers to re-download content
    that has already been replaced.
    """
    week_ago = (now - timedelta(days=7)).isoformat()
    deleted_rows = conn.execute(
        "SELECT al.created_at, al.space_reclaimed_bytes, "
        "mi.title, al.detail, mi.plex_rating_key, mi.media_type "
        "FROM audit_log al "
        "LEFT JOIN media_items mi ON al.media_item_id = mi.id "
        "WHERE al.action='deleted' AND al.created_at >= ? "
        "ORDER BY al.created_at DESC LIMIT 10",
        (week_ago,),
    ).fetchall()

    redownload_rows = conn.execute(
        "SELECT media_item_id, created_at FROM audit_log "
        "WHERE action IN ('re_downloaded', 'downloaded')"
    ).fetchall()
    redownload_times: dict[str, str] = {}
    for rd in redownload_rows:
        key = rd["media_item_id"].lower()
        if key not in redownload_times or rd["created_at"] > redownload_times[key]:
            redownload_times[key] = rd["created_at"]

    items = []
    for row in deleted_rows:
        title = row["title"] or _extract_title_from_detail(row["detail"])

        last_redownload = redownload_times.get(title.lower())
        if last_redownload and last_redownload > row["created_at"]:
            continue

        days_ago = _parse_days_ago(row["created_at"], now)
        if days_ago is None:
            deleted_date = ""
        elif days_ago == 0:
            deleted_date = "today"
        elif days_ago == 1:
            deleted_date = "yesterday"
        else:
            deleted_date = f"{days_ago} days ago"

        rating_key = row["plex_rating_key"] or _extract_rk_from_detail(row["detail"]) or ""
        poster_url = (
            f"{base_url}{sign_poster_url(rating_key, secret_key)}"
            if rating_key and base_url
            else ""
        )

        items.append(
            {
                "title": title,
                "poster_url": poster_url,
                "deleted_date": deleted_date,
                "file_size_bytes": row["space_reclaimed_bytes"] or 0,
                "media_type": row["media_type"] or "movie",
            }
        )

    return items


def _load_storage_stats(conn: sqlite3.Connection, now: datetime) -> tuple[dict, int, int, int]:
    """Build storage stats and reclaimed-space totals.

    Returns ``(storage_dict, reclaimed_week, reclaimed_month, reclaimed_total)``.
    """
    from mediaman.services.infra.storage import get_aggregate_disk_usage

    type_rows = conn.execute(
        "SELECT media_type, SUM(file_size_bytes) AS total FROM media_items GROUP BY media_type"
    ).fetchall()
    raw_types: dict[str, int] = {r["media_type"]: (r["total"] or 0) for r in type_rows}
    by_type: dict[str, int] = {
        "movie": raw_types.get("movie", 0),
        "show": (
            raw_types.get("tv_season", 0) + raw_types.get("tv", 0) + raw_types.get("season", 0)
        ),
        "anime": (raw_types.get("anime_season", 0) + raw_types.get("anime", 0)),
    }
    used_bytes = sum(by_type.values())
    total_bytes = used_bytes
    free_bytes = 0
    try:
        disk = get_aggregate_disk_usage("/media")
        total_bytes = disk["total_bytes"]
        used_bytes = disk["used_bytes"]
        free_bytes = disk["free_bytes"]
    except OSError:
        logger.warning("Failed to fetch disk usage for newsletter", exc_info=True)

    storage = {
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "by_type": by_type,
    }

    def _reclaimed_since(since_iso: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(space_reclaimed_bytes), 0) AS total "
            "FROM audit_log WHERE action='deleted' AND created_at >= ?",
            (since_iso,),
        ).fetchone()
        return row["total"] if row else 0

    week_start = (now - timedelta(days=7)).isoformat()
    month_start = (now - timedelta(days=30)).isoformat()
    reclaimed_week = _reclaimed_since(week_start)
    reclaimed_month = _reclaimed_since(month_start)
    reclaimed_total_row = conn.execute(
        "SELECT COALESCE(SUM(space_reclaimed_bytes), 0) AS total "
        "FROM audit_log WHERE action='deleted'"
    ).fetchone()
    reclaimed_total = reclaimed_total_row["total"] if reclaimed_total_row else 0

    return storage, reclaimed_week, reclaimed_month, reclaimed_total


def _load_recommendations(conn: sqlite3.Connection) -> list[dict]:
    """Load the most recent suggestion batch if the feature is enabled.

    Returns an empty list when suggestions are disabled or there are no rows.
    Builds explicit dicts with only the fields the template needs, avoiding
    leakage of internal DB columns via ``**dict(row)`` spreading.
    """
    rec_enabled_row = conn.execute(
        "SELECT value FROM settings WHERE key='suggestions_enabled'"
    ).fetchone()
    if rec_enabled_row and rec_enabled_row["value"] == "false":
        return []

    batch_row = conn.execute(
        "SELECT DISTINCT batch_id FROM suggestions WHERE batch_id IS NOT NULL "
        "ORDER BY batch_id DESC LIMIT 1"
    ).fetchone()
    if not batch_row:
        return []

    rows = conn.execute(
        "SELECT id, title, media_type, category, description, reason, "
        "poster_url, tmdb_id, rating, rt_rating "
        "FROM suggestions WHERE batch_id = ? ORDER BY category DESC, id",
        (batch_row["batch_id"],),
    ).fetchall()

    return [
        {
            "id": r["id"],
            "title": r["title"],
            "media_type": r["media_type"],
            "category": r["category"],
            "description": r["description"],
            "reason": r["reason"],
            "poster_url": r["poster_url"],
            "tmdb_id": r["tmdb_id"],
            "rating": r["rating"],
            "rt_rating": r["rt_rating"],
        }
        for r in rows
    ]
