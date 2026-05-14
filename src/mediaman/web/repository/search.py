"""Repository functions for the search route's ratings-cache reads/writes.

Owns the :class:`ratings_cache` SQL surface used by
:mod:`mediaman.web.routes.search._enrichment` when annotating TMDB
search results with the OMDb rating cache.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class RatingsCacheRow:
    """A single row from the ``ratings_cache`` table."""

    tmdb_id: int
    media_type: str
    imdb_rating: str | None
    rt_rating: str | None
    metascore: str | None
    fetched_at: str


def fetch_ratings_cache(
    conn: sqlite3.Connection, keys: list[tuple[int, str]]
) -> list[RatingsCacheRow]:
    """Return cached ratings rows for the given ``(tmdb_id, media_type)`` keys.

    Uses a tuple-IN clause so the query stays a single round-trip
    regardless of the number of keys.  Returns an empty list when
    ``keys`` is empty (no SQL is issued in that case).
    """
    if not keys:
        return []
    # rationale: placeholders is purely "(?, ?)" * len(keys) — no user value ever enters the SQL text
    placeholders = ",".join(["(?, ?)"] * len(keys))
    flat: list[object] = []
    for tmdb_id, media_type in keys:
        flat.extend([tmdb_id, media_type])
    rows = conn.execute(
        f"SELECT tmdb_id, media_type, imdb_rating, rt_rating, metascore, fetched_at "
        f"FROM ratings_cache WHERE (tmdb_id, media_type) IN ({placeholders})",
        flat,
    ).fetchall()
    return [
        RatingsCacheRow(
            tmdb_id=int(r["tmdb_id"]),
            media_type=r["media_type"],
            imdb_rating=r["imdb_rating"],
            rt_rating=r["rt_rating"],
            metascore=r["metascore"],
            fetched_at=r["fetched_at"],
        )
        for r in rows
    ]


def upsert_ratings_cache(
    conn: sqlite3.Connection, rows: list[tuple[int, str, str | None, str | None, str | None, str]]
) -> None:
    """Insert-or-replace a batch of ``ratings_cache`` rows.

    Each tuple is ``(tmdb_id, media_type, imdb_rating, rt_rating,
    metascore, fetched_at)``.  Commits on success; the caller catches
    ``sqlite3.Error`` to log and continue.
    """
    if not rows:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO ratings_cache "
        "(tmdb_id, media_type, imdb_rating, rt_rating, metascore, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


__all__ = [
    "RatingsCacheRow",
    "fetch_ratings_cache",
    "upsert_ratings_cache",
]
