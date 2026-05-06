"""Scheduled-deletion card loader and notified-flag bookkeeping."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from mediaman.crypto import sign_poster_url

from ._time import _parse_days_ago


def _load_scheduled_items(
    conn: sqlite3.Connection,
    secret_key: str,
    base_url: str,
    now: datetime,
    mark_notified: bool,
) -> list[dict]:
    """Query and build the scheduled-deletion card list.

    When *mark_notified* is True (automated send), only unnotified rows are
    included.  When False (manual send), all pending-token rows are included.
    """
    if mark_notified:
        rows = conn.execute(
            "SELECT sa.id, sa.media_item_id, sa.token, sa.is_reentry, "
            "mi.title, mi.media_type, mi.season_number, mi.plex_rating_key, mi.file_size_bytes, "
            "mi.added_at, mi.last_watched_at "
            "FROM scheduled_actions sa "
            "JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.action='scheduled_deletion' AND sa.notified=0"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT sa.id, sa.media_item_id, sa.token, sa.is_reentry, "
            "mi.title, mi.media_type, mi.season_number, mi.plex_rating_key, mi.file_size_bytes, "
            "mi.added_at, mi.last_watched_at "
            "FROM scheduled_actions sa "
            "JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.action='scheduled_deletion' AND sa.token_used=0"
        ).fetchall()

    items = []
    for row in rows:
        added_days_ago = _parse_days_ago(row["added_at"], now)
        rating_key = row["plex_rating_key"] or ""
        poster_url = (
            f"{base_url}{sign_poster_url(rating_key, secret_key)}"
            if rating_key and base_url
            else ""
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

        items.append(
            {
                "title": row["title"],
                "media_type": media_type,
                "type_label": type_label,
                "poster_url": poster_url,
                "file_size_bytes": row["file_size_bytes"] or 0,
                "added_days_ago": added_days_ago,
                "last_watched_info": last_watched_info,
                "keep_url": f"{base_url}/keep/{row['token']}",
                "is_reentry": bool(row["is_reentry"]),
                "_action_id": row["id"],
            }
        )

    # Sort oldest first (most days ago at the top)
    items.sort(key=lambda x: x.get("added_days_ago") or 0, reverse=True)
    return items


def _mark_notified(
    conn: sqlite3.Connection,
    scheduled_items: list[dict],
    *,
    active_recipients: list[str] | None = None,
) -> None:
    """Mark scheduled action rows as notified=1, but only when every active
    recipient has been delivered to.

    Asserts all ids are integers before building the parameterised query so a
    non-integer id (e.g. from a corrupt row) surfaces as a clear error rather
    than silently passing a string through to the SQL engine.

    The legacy callsites that pass ``active_recipients=None`` keep the
    old "any send -> mark all" behaviour — used by the manual-resend
    path which never set ``mark_notified``.
    """
    action_ids = [int(item["_action_id"]) for item in scheduled_items]
    if not action_ids:
        return

    if not active_recipients:
        # Legacy behaviour: caller didn't supply a recipient set, so we
        # cannot validate per-recipient state.  Mark everything.
        placeholders = ",".join("?" * len(action_ids))
        conn.execute(
            f"UPDATE scheduled_actions SET notified=1 WHERE id IN ({placeholders})",
            action_ids,
        )
        conn.commit()
        return

    expected = set(active_recipients)
    fully_delivered: list[int] = []
    # rationale: batched IN-clause replaces N+1 query
    placeholders = ",".join("?" * len(action_ids))
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
        conn.execute(
            f"UPDATE scheduled_actions SET notified=1 WHERE id IN ({placeholders})",
            fully_delivered,
        )
        conn.commit()
