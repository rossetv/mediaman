"""Scheduled-deletion card loader and notified-flag bookkeeping."""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime

from mediaman.crypto import sign_poster_url
from mediaman.crypto.tokens import rebuild_keep_token, validate_keep_token
from mediaman.services.scheduled_actions import parse_execute_at

from ._time import _parse_days_ago
from ._types import ScheduledNewsletterItem

logger = logging.getLogger(__name__)


def _keep_url(row: sqlite3.Row, now: datetime, base_url: str, secret_key: str) -> str:
    """Return the row's /keep link, or "" (no Keep button) when the keep page would refuse it.

    That is a row past its deadline or already being deleted, or one whose token cannot be
    rebuilt or has expired.
    """
    # Same rule as is_pending_unexpired (the action is already scheduled_deletion here).
    if parse_execute_at(row["execute_at"], default=now) < now or row["delete_status"] not in (
        None,
        "pending",
    ):
        return ""
    try:
        token = rebuild_keep_token(
            media_item_id=row["media_item_id"],
            action_id=row["id"],
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            execute_at=datetime.fromisoformat(row["execute_at"]),
            token_hash=row["token_hash"],
            secret_key=secret_key,
        )
    except (TypeError, ValueError):  # unparsable or missing dates
        token = None
    # validate_keep_token also rejects a token whose exp has passed.
    if token is None or validate_keep_token(token, secret_key) is None:
        logger.warning("newsletter.keep_link_unavailable action_id=%s", row["id"])
        return ""
    return f"{base_url}/keep/{token}"


def _days_left(row: sqlite3.Row, now: datetime) -> int:
    """Whole days until the row's deadline, rounded up, never below 0.

    A deadline that is already past, or unreadable, counts as 0 (the same "treat as
    expired" fallback the keep page uses).
    """
    remaining = parse_execute_at(row["execute_at"], default=now) - now
    return max(0, math.ceil(remaining.total_seconds() / 86400))


def _build_scheduled_card(
    row: sqlite3.Row,
    now: datetime,
    base_url: str,
    secret_key: str,
) -> ScheduledNewsletterItem:
    added_days_ago = _parse_days_ago(row["added_at"], now)
    rating_key = row["plex_rating_key"] or ""
    poster_url = (
        f"{base_url}{sign_poster_url(rating_key, secret_key)}" if rating_key and base_url else ""
    )

    last_watched_info = None
    lw_raw = row["last_watched_at"]
    if lw_raw:
        lw_days = _parse_days_ago(lw_raw, now)
        if lw_days is not None:
            if lw_days == 0:
                last_watched_info = "Watched today"
            elif lw_days == 1:
                last_watched_info = "Watched yesterday"
            else:
                last_watched_info = f"Watched {lw_days} days ago"

    media_type = row["media_type"] or "movie"
    season_num = row["season_number"]
    if media_type in ("tv_season", "season", "tv"):
        type_label = f"TV · Season {season_num}" if season_num else "TV"
    elif media_type in ("anime_season", "anime"):
        type_label = f"Anime · Season {season_num}" if season_num else "Anime"
    else:
        type_label = "Movie"

    return {
        "title": row["title"],
        "media_type": media_type,
        "type_label": type_label,
        "poster_url": poster_url,
        "file_size_bytes": row["file_size_bytes"] or 0,
        "added_days_ago": added_days_ago,
        "days_left": _days_left(row, now),
        "last_watched_info": last_watched_info,
        "keep_url": _keep_url(row, now, base_url, secret_key),
        "is_reentry": bool(row["is_reentry"]),
        "_action_id": row["id"],
    }


def _load_scheduled_items(
    conn: sqlite3.Connection,
    secret_key: str,
    base_url: str,
    now: datetime,
    mark_notified: bool,
) -> list[ScheduledNewsletterItem]:
    """Query the scheduled-deletion cards: only unnotified rows when *mark_notified*, all
    pending-token rows otherwise.

    Sorted oldest first; items with a corrupt timestamp (added_days_ago None) sort last
    as unknown age rather than as most recent.
    """
    if mark_notified:
        rows = conn.execute(
            "SELECT sa.id, sa.media_item_id, sa.scheduled_at, sa.execute_at, sa.token_hash, "
            "sa.is_reentry, sa.delete_status, "
            "mi.title, mi.media_type, mi.season_number, mi.plex_rating_key, mi.file_size_bytes, "
            "mi.added_at, mi.last_watched_at "
            "FROM scheduled_actions sa "
            "JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.action='scheduled_deletion' AND sa.notified=0"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT sa.id, sa.media_item_id, sa.scheduled_at, sa.execute_at, sa.token_hash, "
            "sa.is_reentry, sa.delete_status, "
            "mi.title, mi.media_type, mi.season_number, mi.plex_rating_key, mi.file_size_bytes, "
            "mi.added_at, mi.last_watched_at "
            "FROM scheduled_actions sa "
            "JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.action='scheduled_deletion' AND sa.token_used=0"
        ).fetchall()

    items = [_build_scheduled_card(row, now, base_url, secret_key) for row in rows]

    # Sort oldest-first; items with None added_days_ago (corrupt timestamp)
    # sort to the end (key=-1) rather than mapping to 0 which would silently
    # promote them to "most recent".
    def _sort_key(x: ScheduledNewsletterItem) -> int:
        v = x.get("added_days_ago")
        return v if v is not None else -1

    items.sort(key=_sort_key, reverse=True)
    return items


def _mark_notified(
    conn: sqlite3.Connection,
    scheduled_items: list[ScheduledNewsletterItem],
    *,
    active_recipients: list[str],
) -> None:
    """Set notified=1 for action ids delivered to every active subscriber.

    An id qualifies when its non-NULL sent_at rows in newsletter_deliveries cover
    *active_recipients*; partially delivered items stay at 0 for the next tick. The
    caller decides which items to include.

    Asserts ids are integers before building the query so a corrupt row fails loudly
    instead of passing a string to SQL.
    """
    action_ids = [int(item["_action_id"]) for item in scheduled_items]
    if not action_ids:
        return

    expected = set(active_recipients)
    fully_delivered: list[int] = []
    placeholders = ",".join("?" * len(action_ids))
    # rationale: placeholder list built from validated integer IDs only; no user input reaches this interpolation
    # Note: the ``recipient`` column name in newsletter_deliveries is the legacy
    # DB column (part of a composite PRIMARY KEY) retained to avoid a destructive migration.
    delivery_rows = conn.execute(
        f"SELECT scheduled_action_id, recipient FROM newsletter_deliveries "
        f"WHERE scheduled_action_id IN ({placeholders}) AND sent_at IS NOT NULL",
        action_ids,
    ).fetchall()
    delivered_by_action: dict[int, set[str]] = {}
    for dr in delivery_rows:
        delivered_by_action.setdefault(dr["scheduled_action_id"], set()).add(dr["recipient"])
    for action_id in action_ids:
        if expected.issubset(delivered_by_action.get(action_id, set())):
            fully_delivered.append(action_id)

    if fully_delivered:
        placeholders = ",".join("?" * len(fully_delivered))
        # rationale: placeholder list built from validated integer IDs only; no user input reaches this interpolation
        # Wrap DML in ``with conn:`` so a crash between UPDATE and
        # commit does not leave the database in an inconsistent state.
        with conn:
            conn.execute(
                f"UPDATE scheduled_actions SET notified=1 WHERE id IN ({placeholders})",
                fully_delivered,
            )
