"""Repository functions for the download routes.

Covers the per-route SQL that backs:

* ``used_download_tokens`` — the persistent single-use store backing
  :mod:`mediaman.web.routes.download._tokens` (the in-memory LRU cache
  itself stays in the route module — it is process state, not SQL).
* ``recent_downloads`` — short-window cache of ``ready`` items used as a
  fallback during status polling when the Arr API lags.
* ``suggestions`` — the per-suggestion enrichment fields fetched when the
  confirm page renders a previously-suggested item.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class RecentDownload:
    """A row from ``recent_downloads`` keyed by ``dl_id``."""

    dl_id: str
    title: str
    poster_url: str


@dataclass(frozen=True)
class SuggestionEnrichment:
    """Enrichment columns fetched from ``suggestions`` for the confirm page."""

    poster_url: str | None
    year: int | None
    description: str | None
    reason: str | None
    rating: float | None
    rt_rating: str | None
    tagline: str | None
    runtime: int | None
    genres: str | None
    cast_json: str | None
    director: str | None
    trailer_key: str | None
    imdb_rating: str | None
    metascore: str | None


def claim_download_token(conn: sqlite3.Connection, *, digest: str, exp: int) -> bool:
    """Insert *digest* into ``used_download_tokens``.

    Returns ``True`` when the row was inserted (this caller wins the
    race) and ``False`` when a sibling worker or earlier request had
    already claimed the slot.  ``INSERT OR IGNORE`` plus the unique
    constraint on ``token_hash`` give us atomic claim semantics.
    """
    expires_at = datetime.fromtimestamp(exp, tz=UTC).isoformat()
    used_at = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO used_download_tokens "
        "(token_hash, expires_at, used_at) VALUES (?, ?, ?)",
        (digest, expires_at, used_at),
    )
    conn.commit()
    return cursor.rowcount == 1


def release_download_token(conn: sqlite3.Connection, digest: str) -> None:
    """Delete *digest* from ``used_download_tokens``."""
    conn.execute("DELETE FROM used_download_tokens WHERE token_hash = ?", (digest,))
    conn.commit()


def purge_expired_download_tokens(conn: sqlite3.Connection, *, now_iso: str) -> None:
    """Delete every ``used_download_tokens`` row whose ``expires_at`` is past."""
    conn.execute("DELETE FROM used_download_tokens WHERE expires_at < ?", (now_iso,))
    conn.commit()


def fetch_recent_download(conn: sqlite3.Connection, dl_id: str) -> RecentDownload | None:
    """Return the cached ``recent_downloads`` row for ``dl_id`` or None."""
    row = conn.execute(
        "SELECT dl_id, title, poster_url FROM recent_downloads WHERE dl_id = ?",
        (dl_id,),
    ).fetchone()
    if row is None:
        return None
    return RecentDownload(
        dl_id=row["dl_id"],
        title=row["title"],
        poster_url=row["poster_url"] or "",
    )


def fetch_suggestion_enrichment(
    conn: sqlite3.Connection, suggestion_id: int
) -> SuggestionEnrichment | None:
    """Return the enrichment columns from ``suggestions`` for the given id."""
    row = conn.execute(
        "SELECT poster_url, year, description, reason, rating, rt_rating, "
        "tagline, runtime, genres, cast_json, director, trailer_key, "
        "imdb_rating, metascore "
        "FROM suggestions WHERE id = ?",
        (suggestion_id,),
    ).fetchone()
    if row is None:
        return None
    return SuggestionEnrichment(
        poster_url=row["poster_url"],
        year=row["year"],
        description=row["description"],
        reason=row["reason"],
        rating=row["rating"],
        rt_rating=row["rt_rating"],
        tagline=row["tagline"],
        runtime=row["runtime"],
        genres=row["genres"],
        cast_json=row["cast_json"],
        director=row["director"],
        trailer_key=row["trailer_key"],
        imdb_rating=row["imdb_rating"],
        metascore=row["metascore"],
    )


__all__ = [
    "RecentDownload",
    "SuggestionEnrichment",
    "claim_download_token",
    "fetch_recent_download",
    "fetch_suggestion_enrichment",
    "purge_expired_download_tokens",
    "release_download_token",
]
