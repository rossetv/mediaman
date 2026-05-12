"""Repository functions for the suggestions (recommendations cache) table.

Centralises reads and writes against ``suggestions`` so the route layer
and the refresh worker share a single query surface.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SuggestionRow:
    """Minimal projection of a suggestions row used by the share-token and download routes."""

    id: int
    title: str
    media_type: str
    tmdb_id: int | None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def fetch_suggestion_by_id(conn: sqlite3.Connection, suggestion_id: int) -> sqlite3.Row | None:
    """Return the full suggestions row for *suggestion_id*, or None.

    Returns the raw sqlite3.Row so callers that need all columns (e.g. the
    download route which passes the row into Arr helpers) get the full set
    without a wide dataclass definition here.
    """
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
    ).fetchone()
    return row


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
