"""Disk-usage aggregation, reclaimed-space totals, recently-deleted cards."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from requests.exceptions import RequestException

from mediaman.core.deleted_media import (
    DeletedMedia,
    fetch_deleted_media,
    redownload_tokens,
    was_redownloaded_after,
)
from mediaman.core.format import rk_from_audit_detail as _extract_rk_from_detail
from mediaman.core.format import title_from_audit_detail as _extract_title_from_detail
from mediaman.crypto import sign_poster_url, sign_tmdb_image_url
from mediaman.services.infra import SafeHTTPError
from mediaman.services.openai.recommendations.repository import SHARED_ONLY
from mediaman.services.openai.recommendations.user_picks import PICKS_LAST, fetch_picks

from ._time import _parse_days_ago
from ._types import DeletedNewsletterItem, NewsletterRecItem, StorageStats

logger = logging.getLogger(__name__)

# As long as the mail's signed posters last: it is read long after it is sent, with no session.
_PICK_POSTER_DAYS = 180


@dataclass(frozen=True, slots=True)
class StorageSummary:
    stats: StorageStats
    reclaimed_week: int
    reclaimed_month: int
    reclaimed_total: int


def _build_redownload_index(conn: sqlite3.Connection, week_ago: str) -> dict[str, str]:
    """Map media_item_id to the most recent re/download timestamp, to exclude already
    re-downloaded items from the deleted cards. One query replaces a per-row lookup
    (N+1).

    Keys are lower-cased media_item_id values: a Plex numeric id, a ``tmdb:<id>`` /
    ``tvdb:<id>[:s<n>]`` token (see redownload_tokens) or, for a Search re-add, the title.
    *week_ago* bounds the scan to the deleted-items window; an item outside it would not be
    listed anyway.
    """
    redownload_rows = conn.execute(
        "SELECT media_item_id, created_at FROM audit_log "
        "WHERE action IN ('re_downloaded', 'downloaded') AND created_at >= ?",
        (week_ago,),
    ).fetchall()
    redownload_times: dict[str, str] = {}
    for rd in redownload_rows:
        # Lower-cased like the storage panel, so a title matches whatever its case.
        key = str(rd["media_item_id"]).lower()
        if key not in redownload_times or rd["created_at"] > redownload_times[key]:
            redownload_times[key] = rd["created_at"]
    return redownload_times


def _format_deleted_card(
    row: sqlite3.Row,
    stored: DeletedMedia | None,
    title: str,
    now: datetime,
    base_url: str,
    secret_key: str,
) -> DeletedNewsletterItem:
    """Build one deleted-item card from a DB row and its identity row (pure, no DB access)."""
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
    media_type = stored.media_type if stored else row["media_type"]

    return {
        "title": title,
        "poster_url": poster_url,
        "deleted_date": deleted_date,
        "file_size_bytes": row["space_reclaimed_bytes"] or 0,
        "media_type": "movie" if media_type == "movie" else "tv",
        "tmdb_id": stored.tmdb_id if stored else None,
        "audit_id": row["audit_id"],
        "season_number": stored.season_number if stored else None,
    }


def _fill_show_tmdb_ids(
    conn: sqlite3.Connection,
    secret_key: str,
    cards: list[DeletedNewsletterItem],
    stored_by_audit: dict[int, DeletedMedia],
) -> None:
    """Give each card of a show recorded with only its TVDB id the TMDB id TMDB has for it.

    The id is stored with the deletion too: a season's link is matched against it.
    """
    from mediaman.services.media_meta.tmdb import TmdbClient

    lacking: list[tuple[DeletedNewsletterItem, int]] = []
    for card in cards:
        stored = stored_by_audit.get(card["audit_id"])
        if stored and stored.tvdb_id and not stored.tmdb_id and card["media_type"] != "movie":
            lacking.append((card, stored.tvdb_id))
    client = TmdbClient.from_db(conn, secret_key) if lacking else None
    if client is None:
        return
    found: dict[int, int | None] = {}
    try:
        for _, tvdb_id in lacking:
            if tvdb_id not in found:
                found[tvdb_id] = client.find_by_tvdb(tvdb_id)
    except (SafeHTTPError, RequestException, ValueError):
        logger.warning("newsletter.show_ids_unavailable", exc_info=True)
    named = [(card, tmdb_id) for card, tvdb_id in lacking if (tmdb_id := found.get(tvdb_id))]
    try:
        with conn:
            conn.executemany(
                "UPDATE deleted_media SET tmdb_id = ? WHERE audit_id = ?",
                [(tmdb_id, card["audit_id"]) for card, tmdb_id in named],
            )
    except sqlite3.Error:
        logger.warning("newsletter.show_ids_not_stored", exc_info=True)
        return
    for card, tmdb_id in named:
        card["tmdb_id"] = tmdb_id


def _load_deleted_items(
    conn: sqlite3.Connection,
    secret_key: str,
    base_url: str,
    now: datetime,
) -> list[DeletedNewsletterItem]:
    """Query the recently-deleted cards (last 7 days), excluding items re-downloaded after
    deletion.

    The type and ids are the ones recorded with the deletion. The subscriber loop mints a
    re-download URL only when there is a tmdb_id: a title alone could fetch another title
    of the same name.
    """
    week_ago = (now - timedelta(days=7)).isoformat()
    deleted_rows = conn.execute(
        "SELECT al.id AS audit_id, al.created_at, al.space_reclaimed_bytes, al.media_item_id, "
        "mi.title, al.detail, mi.plex_rating_key, mi.media_type "
        "FROM audit_log al "
        "LEFT JOIN media_items mi ON al.media_item_id = mi.id "
        "WHERE al.action='deleted' AND al.created_at >= ? "
        "ORDER BY al.created_at DESC LIMIT 10",
        (week_ago,),
    ).fetchall()

    redownload_times = _build_redownload_index(conn, week_ago)
    stored_by_audit = fetch_deleted_media(conn, [r["audit_id"] for r in deleted_rows])

    cards: list[DeletedNewsletterItem] = []
    for row in deleted_rows:
        stored = stored_by_audit.get(row["audit_id"])
        title = (
            row["title"]
            or (stored.title if stored else None)
            or _extract_title_from_detail(row["detail"])
        )

        # Look up by plex_rating_key (== media_item_id in audit_log),
        # which is the same key used when building the redownload index.
        plex_key = row["plex_rating_key"] or str(row["media_item_id"] or "")
        keys = [plex_key] if plex_key else []
        keys += redownload_tokens(stored)
        keys.append(title)
        keys = [k.lower() for k in keys]
        if was_redownloaded_after(row["created_at"], keys, redownload_times):
            continue

        cards.append(_format_deleted_card(row, stored, title, now, base_url, secret_key))

    _fill_show_tmdb_ids(conn, secret_key, cards, stored_by_audit)
    return cards


def _load_storage_stats(conn: sqlite3.Connection, now: datetime) -> StorageSummary:
    from mediaman.services.infra import get_aggregate_disk_usage, get_media_path
    from mediaman.services.infra.storage.disk_reads import media_root_reads

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
        disk = media_root_reads.read(get_media_path(), get_aggregate_disk_usage)
        if disk is None:
            raise OSError("the media folder could not be read in time")
        total_bytes = disk["total_bytes"]
        used_bytes = disk["used_bytes"]
        free_bytes = disk["free_bytes"]
    except OSError:
        logger.warning("newsletter.storage_stats_failed", exc_info=True)

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


def _load_recommendations(
    conn: sqlite3.Connection,
    *,
    check_enabled: bool = True,
    base_url: str = "",
    secret_key: str = "",
) -> list[NewsletterRecItem]:
    """Load the most recent batch of the shared suggestions if the feature is enabled, else [].
    One with no TMDB id is left out: nothing vouches for what it is.

    Builds explicit dicts to avoid leaking internal columns via **dict(row).
    check_enabled=False skips the suggestions_enabled query when the caller already
    checked it.
    """
    if check_enabled:
        rec_enabled_row = conn.execute(
            "SELECT value FROM settings WHERE key='suggestions_enabled'"
        ).fetchone()
        if rec_enabled_row and rec_enabled_row["value"] == "false":
            return []

    batch_row = conn.execute(
        "SELECT DISTINCT batch_id FROM suggestions "
        f"WHERE batch_id IS NOT NULL AND {SHARED_ONLY} "
        "ORDER BY batch_id DESC LIMIT 1"
    ).fetchone()
    if not batch_row:
        return []

    rows = conn.execute(
        "SELECT id, title, media_type, category, description, reason, "
        "poster_url, tmdb_id, rating, rt_rating "
        f"FROM suggestions WHERE batch_id = ? AND {SHARED_ONLY}"
        " AND tmdb_id IS NOT NULL ORDER BY id",
        (batch_row["batch_id"],),
    ).fetchall()
    return [_rec_item(r, r["id"], base_url, secret_key) for r in rows]


def load_own_picks(
    conn: sqlite3.Connection,
    emails: Iterable[str],
    now: datetime,
    *,
    base_url: str = "",
    secret_key: str = "",
) -> dict[str, list[NewsletterRecItem]]:
    """The latest own picks with a TMDB id, not over 28 days old, of each address that is
    exactly one person's, by address.

    A title they rated a strong no is left out, and one they rated down comes last.
    An address of nobody, or of more than one person, has none. A card holds neither the
    pick's id nor the titles that led to it: the email and its links can be passed on.
    """
    found: dict[str, list[NewsletterRecItem]] = {}
    oldest = (now - PICKS_LAST).strftime("%Y-%m-%d")
    for email in emails:
        people = conn.execute(
            "SELECT id, username FROM admin_users WHERE email = ? COLLATE NOCASE", (email.strip(),)
        ).fetchall()
        if len(people) == 1:
            picks = fetch_picks(conn, people[0]["username"], latest_only=True)
            levels = {
                (r["media_type"], r["tmdb_id"]): r["level"]
                for r in conn.execute(
                    "SELECT media_type, tmdb_id, level FROM user_ratings"
                    " WHERE user_id = ? AND level < 0",
                    (people[0]["id"],),
                )
            }
            picks.sort(key=lambda p: (p["media_type"], p["tmdb_id"]) in levels)
            found[email] = [
                _rec_item(p, None, base_url, secret_key)
                for p in picks
                if p["tmdb_id"]
                and str(p["batch_id"]) >= oldest
                and levels.get((p["media_type"], p["tmdb_id"])) != -2
            ]
    return found


def _pick_poster(poster_url: str | None, base_url: str, secret_key: str) -> str | None:
    """A TMDB poster as this site's signed address when the site address is known, so TMDB
    never learns who opens the mail; anything else as it is.
    """
    if not base_url or not secret_key:
        return poster_url
    expires_at = int(time.time()) + _PICK_POSTER_DAYS * 86400
    signed = sign_tmdb_image_url(poster_url, "w342", secret_key, expires_at=expires_at)
    return f"{base_url}{signed}" if signed else poster_url


def _rec_item(
    row: Mapping[str, Any] | sqlite3.Row, rec_id: int | None, base_url: str, secret_key: str
) -> NewsletterRecItem:
    return {
        "id": rec_id,
        "title": row["title"],
        "media_type": row["media_type"],
        "category": row["category"],
        "description": row["description"],
        "reason": row["reason"],
        "poster_url": _pick_poster(row["poster_url"], base_url, secret_key),
        "tmdb_id": row["tmdb_id"],
        "rating": row["rating"],
        "rt_rating": row["rt_rating"],
    }
