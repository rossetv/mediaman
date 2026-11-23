"""SQL read of the plays people made in mediaman, for the watch clock."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from mediaman.core.time import parse_iso_utc


def last_credited_by_media_item(conn: sqlite3.Connection) -> dict[str, datetime]:
    """Return the latest credited play of each media item across all people.

    Keyed by ``media_items.id`` (a movie, or the season an episode belongs
    to), for the scanner to treat like a Plex watch.
    """
    rows = conn.execute(
        "SELECT media_item_id, MAX(credited_at) AS credited_at FROM watch_progress"
        " WHERE media_item_id IS NOT NULL AND credited_at IS NOT NULL"
        " GROUP BY media_item_id"
    ).fetchall()
    credited: dict[str, datetime] = {}
    for row in rows:
        when = parse_iso_utc(row["credited_at"])
        if when is not None:
            credited[row["media_item_id"]] = when
    return credited
