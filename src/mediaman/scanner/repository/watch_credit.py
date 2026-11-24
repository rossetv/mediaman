"""SQL of when items were last watched, for the watch clock."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta

from mediaman.core.time import now_utc, parse_iso_utc
from mediaman.scanner.repository.rekeyed import (
    copies,
    fetch_title,
    ids_conflict,
    ids_match,
    is_film_or_numbered,
    other_keys,
    title_files,
    title_ids,
)

# A watch older than the longest inactivity setting (365 days) keeps nothing.
_WATCH_KEPT_FOR = timedelta(days=366)

# The last watch of a title ``o``, with the plays made in mediaman a scan may not have stored yet.
_LATEST = (
    "MAX(COALESCE(o.last_watched_at, ''), COALESCE("
    " (SELECT MAX(wp.credited_at) FROM watch_progress wp WHERE wp.media_item_id = o.id), ''))"
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


def carry_last_watched(
    conn: sqlite3.Connection, media_id: str, *, every_library_read: bool = True
) -> None:
    """Give *media_id* the last watch of the same title under another key, or its own set aside.

    Only if it is later.

    Plex gives a title it removed and found again a new key, and its plays stay
    with the old one. The old key is in the library, or set aside by
    save_pruned_watches. Run it once the title is stored and before it is evaluated.
    """
    title = fetch_title(conn, media_id)
    if title is None:
        return
    watches = [
        # rationale: the interpolated expression is a fixed string; every value is bound
        conn.execute(  # nosec B608
            f"SELECT {_LATEST} AS at FROM media_items o WHERE o.id = ?", (other["id"],)
        ).fetchone()["at"]
        for other in [
            *other_keys(conn, media_id, ()),
            *copies(conn, title, every_library_read=every_library_read),
        ]
    ]
    tvdb_id, tmdb_id = title_ids(title)
    watches += [
        gone["last_watched_at"]
        for gone in conn.execute(
            "SELECT * FROM pruned_watches WHERE media_item_id = ? "
            "OR (file_path = ? AND file_path != '' AND season_number IS ?) "
            "OR (tvdb_id = ? AND season_number IS ?) "
            "OR (COALESCE(tmdb_id, last_tmdb_id) = ? AND season_number IS ?)",
            (
                media_id,
                *(title["file_path"], title["season_number"]),
                *(tvdb_id, title["season_number"]),
                *(tmdb_id, title["season_number"]),
            ),
        ).fetchall()
        if gone["media_item_id"] == media_id
        or _was_there(conn, gone, title)
        or _was_a_copy(conn, gone, title)
    ]
    latest = max(watches, default="")
    if latest:
        conn.execute(
            "UPDATE media_items SET last_watched_at = MAX(?, COALESCE(last_watched_at, ''))"
            " WHERE id = ?",
            (latest, media_id),
        )


def _was_a_copy(conn: sqlite3.Connection, gone: sqlite3.Row, title: sqlite3.Row) -> bool:
    """Return True if *gone* of pruned_watches, still not in the library, has the ids of *title*."""
    return (
        is_film_or_numbered(title)
        and ids_match(title_ids(gone), title_ids(title))
        and fetch_title(conn, gone["media_item_id"]) is None
    )


def _was_there(conn: sqlite3.Connection, gone: sqlite3.Row, title: sqlite3.Row) -> bool:
    """Return True if *gone* of pruned_watches was at the place of *title*, and no other season.

    A film's path is its file. A season's files are gone with it, but for those
    of the episodes someone has progress in.
    """
    if not title["file_path"] or gone["file_path"] != title["file_path"]:
        return False
    if title["season_number"] is None or not ids_conflict(gone, title):
        return True
    files = title_files(title)
    return any(
        row["episode_path"] in files
        for row in conn.execute(
            "SELECT episode_path FROM pruned_progress WHERE media_item_id = ?",
            (gone["media_item_id"],),
        )
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
        " (media_item_id, file_path, season_number, show_title, tmdb_id, tvdb_id, last_tmdb_id,"
        " last_watched_at)"
        " SELECT o.id, o.file_path, o.season_number, o.show_title, o.tmdb_id, o.tvdb_id,"
        f" o.last_tmdb_id, {_LATEST} FROM media_items o"
        f" WHERE o.id = ? AND {_LATEST} != ''",
        [(media_id,) for media_id in media_ids],
    )
