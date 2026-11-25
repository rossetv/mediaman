"""What one person said of titles, and asked for: what their plays alone do not tell.

Every read takes one person and reads only that person's rows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_ASKED_MAX = 20
_SEARCHES_MAX = 20


@dataclass(frozen=True, slots=True)
class Rating:
    media_type: str
    tmdb_id: int
    level: int
    title: str | None
    year: int | None


@dataclass(frozen=True, slots=True)
class Seen:
    """A title marked watched. It is not told to the AI, only left out of what it names."""

    media_type: str
    tmdb_id: int
    title: str | None
    year: int | None


@dataclass(frozen=True, slots=True)
class Taste:
    #: False when the person keeps what is below from the AI.
    may_send: bool
    ratings: tuple[Rating, ...]
    #: (media type, title), newest first.
    asked_for: tuple[tuple[str, str], ...]
    searches: tuple[str, ...]
    seen: tuple[Seen, ...] = ()

    @property
    def levels(self) -> dict[tuple[str, int], int]:
        return {(r.media_type, r.tmdb_id): r.level for r in self.ratings}


def read_taste(conn: sqlite3.Connection, username: str) -> Taste:
    user = conn.execute(
        "SELECT id, ai_history FROM admin_users WHERE username = ?", (username,)
    ).fetchone()
    if user is None:
        return Taste(False, (), (), ())
    ratings = conn.execute(
        "SELECT media_type, tmdb_id, level, title, year FROM user_ratings WHERE user_id = ?"
        " ORDER BY ABS(level) DESC, updated_at DESC",
        (user["id"],),
    )
    asked = conn.execute(
        "SELECT media_type, title FROM download_notifications"
        " WHERE username = ? AND title != '' GROUP BY media_type, title"
        " ORDER BY MAX(id) DESC LIMIT ?",
        (username, _ASKED_MAX),
    )
    searches = conn.execute(
        "SELECT phrase FROM user_searches WHERE user_id = ? ORDER BY searched_at DESC LIMIT ?",
        (user["id"], _SEARCHES_MAX),
    )
    seen = conn.execute(
        "SELECT media_type, tmdb_id, title, year FROM user_seen_titles WHERE user_id = ?",
        (user["id"],),
    )
    return Taste(
        may_send=bool(user["ai_history"]),
        ratings=tuple(Rating(*row) for row in ratings),
        asked_for=tuple(("movie" if r[0] == "movie" else "tv", r[1]) for r in asked),
        searches=tuple(r["phrase"] for r in searches),
        seen=tuple(Seen(*row) for row in seen),
    )
