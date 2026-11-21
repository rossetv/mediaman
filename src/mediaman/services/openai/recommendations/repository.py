"""Repository functions for the suggestions (recommendations cache) table.

Centralises reads and writes against ``suggestions`` so the route layer
and the refresh worker share a single query surface.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SuggestionRow:
    id: int
    title: str
    media_type: str
    tmdb_id: int | None


@dataclass(frozen=True, slots=True)
class SuggestionDetail:
    id: int
    title: str
    media_type: str
    tmdb_id: int | None
    year: int | None
    description: str | None
    reason: str | None
    poster_url: str | None
    rating: float | None
    rt_rating: str | None  # TEXT column — OMDb returns e.g. "93%"; not a bare integer
    batch_id: str | None  # TEXT column — stores a fixed-width "YYYY-MM-DD" date string
    downloaded_at: str | None
    created_at: str


def fetch_suggestion_by_id(conn: sqlite3.Connection, suggestion_id: int) -> SuggestionDetail | None:
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


def mark_downloaded(conn: sqlite3.Connection, suggestion_id: int, downloaded_at: str) -> None:
    conn.execute(
        "UPDATE suggestions SET downloaded_at = ? WHERE id = ?",
        (downloaded_at, suggestion_id),
    )
