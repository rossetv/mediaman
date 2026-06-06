"""Repository functions for the suggestions (recommendations cache) table.

Centralises reads and writes against ``suggestions`` so the route layer
and the refresh worker share a single query surface.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SuggestionRow:
    """Minimal projection of a suggestions row used by the share-token and download routes."""

    id: int
    title: str
    media_type: str
    tmdb_id: int | None


@dataclass(frozen=True, slots=True)
class SuggestionDetail:
    """Full projection of a suggestions row used by the download trigger routes.

    Covers the fields consumed by ``_add_rec_to_radarr``, ``_add_rec_to_sonarr``,
    and the outer ``api_download_recommendation`` handler.
    """

    id: int
    title: str
    media_type: str
    tmdb_id: int | None
    year: int | None
    description: str | None
    reason: str | None
    poster_url: str | None
    rating: float | None
    rt_rating: int | None
    batch_id: int | None
    downloaded_at: str | None
    created_at: str


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def fetch_suggestion_by_id(conn: sqlite3.Connection, suggestion_id: int) -> SuggestionDetail | None:
    """Return a typed projection of a suggestions row for *suggestion_id*, or None.

    Returns a :class:`SuggestionDetail` dataclass covering the fields used by
    the download-trigger route (§9.5: repository returns dataclasses, not raw rows).
    """
    row = conn.execute(
        "SELECT id, title, media_type, tmdb_id, year, description, reason, poster_url, "
        "rating, rt_rating, batch_id, downloaded_at, created_at "
        "FROM suggestions WHERE id = ?",
        (suggestion_id,),
    ).fetchone()
    if row is None:
        return None
    return SuggestionDetail(
        id=row["id"],
        title=row["title"],
        media_type=row["media_type"],
        tmdb_id=row["tmdb_id"],
        year=row["year"],
        description=row["description"],
        reason=row["reason"],
        poster_url=row["poster_url"],
        rating=row["rating"],
        rt_rating=row["rt_rating"],
        batch_id=row["batch_id"],
        downloaded_at=row["downloaded_at"],
        created_at=row["created_at"],
    )


def fetch_suggestion_header(conn: sqlite3.Connection, suggestion_id: int) -> SuggestionRow | None:
    """Return a narrow projection (id, title, media_type, tmdb_id) or None.

    Used by the share-token endpoint which only needs the header fields.
    """
    row = conn.execute(
        "SELECT id, title, media_type, tmdb_id FROM suggestions WHERE id = ?",
        (suggestion_id,),
    ).fetchone()
    if row is None:
        return None
    return SuggestionRow(
        id=row["id"],
        title=row["title"],
        media_type=row["media_type"],
        tmdb_id=row["tmdb_id"],
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def mark_downloaded(conn: sqlite3.Connection, suggestion_id: int, downloaded_at: str) -> None:
    """Stamp ``downloaded_at`` on a suggestions row after a successful download."""
    conn.execute(
        "UPDATE suggestions SET downloaded_at = ? WHERE id = ?",
        (downloaded_at, suggestion_id),
    )
