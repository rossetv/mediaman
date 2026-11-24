"""SQL of when items were last watched, for the watch clock."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta

from mediaman.core.time import now_utc, parse_iso_utc

# A watch older than the longest inactivity setting (365 days) keeps nothing.
_WATCH_KEPT_FOR = timedelta(days=366)

# The last watch of a title ``o``, with the plays made in mediaman a scan may not have stored yet.
_LATEST = (
    "MAX(COALESCE(o.last_watched_at, ''), COALESCE("
    " (SELECT MAX(wp.credited_at) FROM watch_progress wp WHERE wp.media_item_id = o.id), ''))"
)

# A folder may hold the seasons of two shows, so the path and season alone do not say it is sure.
_SURELY_THE_SAME = (
    "o.file_path = mi.file_path AND mi.file_path != '' AND o.season_number IS mi.season_number"
    " AND CASE WHEN o.tmdb_id IS NOT NULL AND mi.tmdb_id IS NOT NULL THEN o.tmdb_id = mi.tmdb_id"
    " WHEN o.tvdb_id IS NOT NULL AND mi.tvdb_id IS NOT NULL THEN o.tvdb_id = mi.tvdb_id"
    " ELSE o.show_title IS mi.show_title END"
)


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


def keep_credited_plays(conn: sqlite3.Connection, username: str) -> None:
    """Fold *username*'s credited plays into ``media_items.last_watched_at``.

    Run before the person is deleted, since their plays go with them and
    a scan may not have stored them yet. Only moves the time forward.
    """
    conn.execute(
        "UPDATE media_items SET last_watched_at = MAX(("
        " SELECT MAX(credited_at) FROM watch_progress"
        " WHERE username = ? AND media_item_id = media_items.id),"
        " COALESCE(last_watched_at, ''))"
        " WHERE id IN (SELECT media_item_id FROM watch_progress"
        " WHERE username = ? AND credited_at IS NOT NULL)",
        (username, username),
    )


def carry_last_watched(conn: sqlite3.Connection, media_id: str) -> None:
    """Give *media_id* the last watch of the same title under another key, or its own set aside.

    Only if it is later.

    Plex gives a title it removed and found again a new key, and its plays stay
    with the old one. The old key is in the library, or set aside by
    save_pruned_watches. Run it once the title is stored and before it is evaluated.
    """
    # rationale: the interpolated expression and condition are fixed strings; every value is bound
    row = conn.execute(  # nosec B608
        "SELECT MAX(at) AS at FROM ("
        f" SELECT {_LATEST} AS at FROM media_items mi"
        f" JOIN media_items o ON {_SURELY_THE_SAME} AND o.id != mi.id WHERE mi.id = ?"
        " UNION ALL SELECT o.last_watched_at FROM media_items mi"
        f" JOIN pruned_watches o ON {_SURELY_THE_SAME} WHERE mi.id = ?"
        " UNION ALL SELECT last_watched_at FROM pruned_watches WHERE media_item_id = ?)",
        (media_id, media_id, media_id),
    ).fetchone()
    if row["at"]:
        conn.execute(
            "UPDATE media_items SET last_watched_at = MAX(?, COALESCE(last_watched_at, ''))"
            " WHERE id = ?",
            (row["at"], media_id),
        )


def save_pruned_watches(conn: sqlite3.Connection, media_ids: Collection[str]) -> None:
    """Set aside when titles about to be removed were last watched, for carry_last_watched.

    Run it in the transaction that removes them, before their plays go.
    """
    conn.execute(
        "DELETE FROM pruned_watches WHERE last_watched_at < ?",
        ((now_utc() - _WATCH_KEPT_FOR).isoformat(),),
    )
    # rationale: the interpolated expression is a fixed string; every value is bound
    conn.executemany(  # nosec B608
        "INSERT OR REPLACE INTO pruned_watches"
        " (media_item_id, file_path, season_number, show_title, tmdb_id, tvdb_id, last_watched_at)"
        " SELECT o.id, o.file_path, o.season_number, o.show_title, o.tmdb_id, o.tvdb_id,"
        f" {_LATEST} FROM media_items o"
        f" WHERE o.id = ? AND o.file_path != '' AND {_LATEST} != ''",
        [(media_id,) for media_id in media_ids],
    )
