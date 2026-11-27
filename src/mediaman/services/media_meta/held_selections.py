"""Plex's own selection of an item's tracks while a start has others selected, kept on disk so
that one a stopped run or a refused put-back left on Plex is put back later, never taken for
Plex's own.

Ids are by Plex's parameter names, ``audioStreamID`` and ``subtitleStreamID``; one left out
was not changed.
"""

from __future__ import annotations

import sqlite3

from mediaman.core.time import now_iso

_COLUMNS = {"audioStreamID": "audio_stream_id", "subtitleStreamID": "subtitle_stream_id"}


def load(conn: sqlite3.Connection) -> list[tuple[str, str, dict[str, str]]]:
    """Every ``(rating key, part id, ids)`` held."""
    rows = conn.execute(
        "SELECT rating_key, part_id, audio_stream_id, subtitle_stream_id FROM plex_held_selections"
    ).fetchall()
    return [
        (
            row["rating_key"],
            row["part_id"],
            {name: row[column] for name, column in _COLUMNS.items() if row[column] is not None},
        )
        for row in rows
    ]


def save(conn: sqlite3.Connection, rating_key: str, part: str, ids: dict[str, str]) -> None:
    """Hold *ids* for the item; with none, forget it."""
    with conn:
        if not ids:
            conn.execute("DELETE FROM plex_held_selections WHERE rating_key = ?", (rating_key,))
            return
        conn.execute(
            "INSERT OR REPLACE INTO plex_held_selections"
            " (rating_key, part_id, audio_stream_id, subtitle_stream_id, held_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                rating_key,
                part,
                ids.get("audioStreamID"),
                ids.get("subtitleStreamID"),
                now_iso(),
            ),
        )
