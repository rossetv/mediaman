"""Repository functions for the search route's ratings-cache reads/writes.

Owns the `ratings_cache` SQL surface used by
`mediaman.web.routes.search._enrichment` when annotating TMDB
search results with the OMDb rating cache.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

_RT = re.compile(r"([0-9]{1,3})%")
_IMDB = re.compile(r"10\.0|[0-9]\.[0-9]")


@dataclass(frozen=True)
class RatingsCacheRow:
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
    """Insert-or-update a batch of ``ratings_cache`` rows.

    Each tuple is ``(tmdb_id, media_type, imdb_rating, rt_rating,
    metascore, fetched_at)``.  A rating the row already holds is kept when the new one is
    empty, so an answer with fewer ratings never wipes good ones.  Commits on success;
    the caller catches ``sqlite3.Error`` to log and continue.
    """
    if not rows:
        return
    with conn:
        conn.executemany(
            "INSERT INTO ratings_cache "
            "(tmdb_id, media_type, imdb_rating, rt_rating, metascore, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (tmdb_id, media_type) DO UPDATE SET "
            "imdb_rating = COALESCE(excluded.imdb_rating, imdb_rating), "
            "rt_rating = COALESCE(excluded.rt_rating, rt_rating), "
            "metascore = COALESCE(excluded.metascore, metascore), "
            "fetched_at = excluded.fetched_at",
            rows,
        )


def card_scores(rt: str | None, imdb: str | None) -> dict[str, object]:
    """A Rotten Tomatoes score as OMDb gives it ("85%") out of 100 and an IMDb rating ("7.8"),
    as a card shows them; only those given in that shape."""
    found = _RT.fullmatch(rt or "")
    scores: dict[str, object] = {}
    if found and int(found[1]) <= 100:
        scores["rt"] = int(found[1])
    if imdb and _IMDB.fullmatch(imdb):
        scores["imdb"] = imdb
    return scores


def held_scores(
    conn: sqlite3.Connection, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], dict[str, object]]:
    """``card_scores`` of the titles ``(media_type, tmdb_id)`` the ratings cache holds any of.
    OMDb is never asked."""
    found: dict[tuple[str, int], dict[str, object]] = {}
    # A few hundred at a time keeps each read well inside SQLite's limit on bound values.
    for start in range(0, len(keys), 400):
        chunk = [(tmdb_id, media_type) for media_type, tmdb_id in keys[start : start + 400]]
        for row in fetch_ratings_cache(conn, chunk):
            scores = card_scores(row.rt_rating, row.imdb_rating)
            if scores:
                found[row.media_type, row.tmdb_id] = scores
    return found


__all__ = [
    "RatingsCacheRow",
    "card_scores",
    "fetch_ratings_cache",
    "held_scores",
    "upsert_ratings_cache",
]
