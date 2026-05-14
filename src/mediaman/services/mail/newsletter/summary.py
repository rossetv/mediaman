"""Disk-usage aggregation, reclaimed-space totals, recently-deleted cards."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from mediaman.core.format import rk_from_audit_detail as _extract_rk_from_detail
from mediaman.core.format import title_from_audit_detail as _extract_title_from_detail
from mediaman.crypto import sign_poster_url

from ._time import _parse_days_ago
from ._types import DeletedNewsletterItem, NewsletterRecItem, StorageStats

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StorageSummary:
    """Disk-usage and reclaimed-space totals for the newsletter.

    Returned by :func:`_load_storage_stats` instead of a 4-tuple so
    callers can access fields by name and static analysis can check them.
    """

    stats: StorageStats
    reclaimed_week: int
    reclaimed_month: int
    reclaimed_total: int


def _build_redownload_index(conn: sqlite3.Connection) -> dict[str, str]:
    """Return a dict mapping lowercased media_item_id → most-recent re/download timestamp.

    Used to exclude items that have already been re-downloaded from the
    recently-deleted card list.  A single query replaces the per-row lookup
    that would otherwise be an N+1 anti-pattern.
    """
    redownload_rows = conn.execute(
        "SELECT media_item_id, created_at FROM audit_log "
        "WHERE action IN ('re_downloaded', 'downloaded')"
    ).fetchall()
    redownload_times: dict[str, str] = {}
    for rd in redownload_rows:
        key = rd["media_item_id"].lower()
        if key not in redownload_times or rd["created_at"] > redownload_times[key]:
            redownload_times[key] = rd["created_at"]
    return redownload_times


def _format_deleted_card(
    row: sqlite3.Row,
    now: datetime,
    base_url: str,
    secret_key: str,
) -> dict[str, object]:
    """Build one deleted-item card dict from a DB row.

    Computes the human-readable deletion date and the signed poster URL.
    The ``tmdb_id`` field is set to ``None`` here and filled in later by
    the batched suggestions query in :func:`_load_deleted_items`.
    Pure function — no DB access.
    """
    title = row["title"] or _extract_title_from_detail(row["detail"])

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
        f"{base_url}{sign_poster_url(rating_key, secret_key)}" if rating_key and base_url else ""
    )
    media_type = row["media_type"] or "movie"

    return {
        "title": title,
        "poster_url": poster_url,
        "deleted_date": deleted_date,
        "file_size_bytes": row["space_reclaimed_bytes"] or 0,
        "media_type": media_type,
        # tmdb_id filled in below via the batched query
        "tmdb_id": None,
    }


def _load_deleted_items(
    conn: sqlite3.Connection,
    secret_key: str,
    base_url: str,
    now: datetime,
) -> list[DeletedNewsletterItem]:
    """Query and build the recently-deleted card list (last 7 days).

    Items that were re-downloaded after their deletion timestamp are silently
    excluded so the newsletter doesn't ask subscribers to re-download content
    that has already been replaced.

    Each item carries a ``tmdb_id`` when one can be resolved from the
    ``suggestions`` table (most deleted items were originally downloaded
    from a recommendation).  The recipient loop only mints a re-download
    URL when ``tmdb_id`` is present, so items with no resolvable id keep
    their button hidden — the public ``/download/<token>`` submit endpoint
    cannot reliably enqueue the right film/show via title-only lookup.

    The ``media_items`` schema does not carry a ``tmdb_id`` column itself,
    so items downloaded outside the recommendation flow get no re-download
    button — there is no reliable identifier to mint one from.
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

    redownload_times = _build_redownload_index(conn)

    # Build the per-row card list (excluding re-downloaded items) and
    # collect the (title, media_type) pairs we need to look up.
    cards: list[dict[str, object]] = []
    lookup_pairs: list[tuple[str, str]] = []
    for row in deleted_rows:
        title = row["title"] or _extract_title_from_detail(row["detail"])

        last_redownload = redownload_times.get(title.lower())
        if last_redownload and last_redownload > row["created_at"]:
            continue

        card = _format_deleted_card(row, now, base_url, secret_key)
        cards.append(card)
        if title is not None:
            lookup_pairs.append((title, card["media_type"]))  # type: ignore[arg-type]

    if not cards:
        return []

    # Batch-fetch tmdb_ids for all remaining items in a single query.
    # §13.3: issuing one SELECT per deleted row is an N+1 anti-pattern.
    # Duplicate (title, media_type) pairs are deduplicated by the dict;
    # rows with None titles were excluded above and fall through with
    # tmdb_id=None (template hides the re-download button for those).
    tmdb_by_title_type: dict[tuple[str, str], int] = {}
    if lookup_pairs:
        unique_pairs = list(dict.fromkeys(lookup_pairs))
        # rationale: placeholder list built from (title, media_type) tuples
        # collected from DB rows; no raw user input reaches this interpolation.
        placeholders = ",".join("(?,?)" for _ in unique_pairs)
        flat_params = [v for pair in unique_pairs for v in pair]
        sugg_rows = conn.execute(
            f"SELECT title, media_type, tmdb_id FROM suggestions "
            f"WHERE (title, media_type) IN ({placeholders}) "
            f"AND tmdb_id IS NOT NULL "
            f"ORDER BY created_at DESC",
            flat_params,
        ).fetchall()
        for sr in sugg_rows:
            key = (sr["title"], sr["media_type"])
            # Keep the first (most-recent) hit per pair.
            if key not in tmdb_by_title_type:
                tmdb_by_title_type[key] = sr["tmdb_id"]

    items: list[DeletedNewsletterItem] = []
    for card in cards:
        card["tmdb_id"] = tmdb_by_title_type.get(
            (card["title"], card["media_type"])  # type: ignore[arg-type]
        )
        items.append(card)  # type: ignore[arg-type]

    return items


def _load_storage_stats(conn: sqlite3.Connection, now: datetime) -> StorageSummary:
    """Build storage stats and reclaimed-space totals.

    Returns a :class:`StorageSummary` dataclass with fields
    ``stats``, ``reclaimed_week``, ``reclaimed_month``, ``reclaimed_total``.
    """
    from mediaman.services.infra import get_aggregate_disk_usage, get_media_path

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
        # Honour ``MEDIAMAN_MEDIA_PATH`` so an operator who runs the
        # container with a custom mount point still sees accurate disk
        # stats in the newsletter, instead of a hard-coded ``/media``
        # which would silently fall through to the file-size fallback.
        disk = get_aggregate_disk_usage(get_media_path())
        total_bytes = disk["total_bytes"]
        used_bytes = disk["used_bytes"]
        free_bytes = disk["free_bytes"]
    except OSError:
        logger.warning("Failed to fetch disk usage for newsletter", exc_info=True)

    storage: StorageStats = {
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

    return StorageSummary(
        stats=storage,
        reclaimed_week=reclaimed_week,
        reclaimed_month=reclaimed_month,
        reclaimed_total=reclaimed_total,
    )


def _load_recommendations(conn: sqlite3.Connection) -> list[NewsletterRecItem]:
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
