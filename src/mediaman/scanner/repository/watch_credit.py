"""SQL read of the plays people made in mediaman, for the watch clock."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from mediaman.core.time import parse_iso_utc


def last_credited_by_media_item(
    conn: sqlite3.Connection, media_ids: Sequence[str]
) -> dict[str, datetime]:
    """Return the latest credited play of each of *media_ids* across all people.

    Keyed by ``media_items.id`` (a movie, or the season an episode belongs
    to), for the scanner to treat like a Plex watch.
    """
    credited: dict[str, datetime] = {}
    for start in range(0, len(media_ids), 500):
        chunk = media_ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT media_item_id, MAX(credited_at) AS credited_at FROM watch_progress"
            f" WHERE media_item_id IN ({placeholders}) AND credited_at IS NOT NULL"
            " GROUP BY media_item_id",
            tuple(chunk),
        ).fetchall()
        for row in rows:
            when = parse_iso_utc(row["credited_at"])
            if when is not None:
                credited[row["media_item_id"]] = when
    return credited
