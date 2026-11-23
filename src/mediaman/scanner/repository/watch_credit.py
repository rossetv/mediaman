"""SQL reads of when items were last watched, for the watch clock."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from mediaman.core.time import parse_iso_utc


def _times_by_media_item(
    conn: sqlite3.Connection, query: str, media_ids: Sequence[str]
) -> dict[str, datetime]:
    """Run *query* (``{placeholders}`` for the ids) over *media_ids* in chunks of 500."""
    found: dict[str, datetime] = {}
    for start in range(0, len(media_ids), 500):
        chunk = media_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(query.format(placeholders=placeholders), tuple(chunk)):
            when = parse_iso_utc(row["at"])
            if when is not None:
                found[row["id"]] = when
    return found


def last_credited_by_media_item(
    conn: sqlite3.Connection, media_ids: Sequence[str]
) -> dict[str, datetime]:
    """Return the latest credited play of each of *media_ids* across all people.

    Keyed by ``media_items.id`` (a movie, or the season an episode belongs
    to), for the scanner to treat like a Plex watch.
    """
    return _times_by_media_item(
        conn,
        "SELECT media_item_id AS id, MAX(credited_at) AS at FROM watch_progress"
        " WHERE media_item_id IN ({placeholders}) AND credited_at IS NOT NULL"
        " GROUP BY media_item_id",
        media_ids,
    )


def last_watched_by_media_item(
    conn: sqlite3.Connection, media_ids: Sequence[str]
) -> dict[str, datetime]:
    """Return the stored ``last_watched_at`` of each of *media_ids* that has one.

    It outlives the plays it came from: a deleted person's plays go with
    them, and Plex may stop returning an old watch.
    """
    return _times_by_media_item(
        conn,
        "SELECT id, last_watched_at AS at FROM media_items"
        " WHERE id IN ({placeholders}) AND last_watched_at IS NOT NULL",
        media_ids,
    )
