"""Repository functions for how each person rated films and shows.

Every read is scoped to one person: a rating is never shown to anyone else, and
nothing that deletes or keeps titles reads it.
"""

from __future__ import annotations

import sqlite3


def levels_for(conn: sqlite3.Connection, user_id: int) -> dict[tuple[str, int], int]:
    """The person's level (-2, -1, 1 or 2) of each title they rated, by media type and TMDB id."""
    rows = conn.execute(
        "SELECT media_type, tmdb_id, level FROM user_ratings WHERE user_id = ?", (user_id,)
    )
    return {(r["media_type"], r["tmdb_id"]): r["level"] for r in rows}
