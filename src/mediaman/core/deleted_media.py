"""Identity of deleted media, kept beside the ``deleted`` audit row.

The audit row only holds a title; this row keeps the TMDB/TVDB id so a
deleted item can be re-downloaded later. Like the audit helpers, nothing
here commits: the caller's transaction covers both rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class DeletedMedia:
    audit_id: int
    media_type: str
    tmdb_id: int | None
    tvdb_id: int | None
    season_number: int | None
    title: str


def record_deleted_media(
    conn: sqlite3.Connection,
    audit_id: int,
    *,
    media_type: str,
    tmdb_id: int | None,
    tvdb_id: int | None,
    season_number: int | None,
    title: str,
) -> None:
    """Insert the identity row for the ``deleted`` audit row *audit_id*."""
    conn.execute(
        "INSERT INTO deleted_media "
        "(audit_id, media_type, tmdb_id, tvdb_id, season_number, title) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (audit_id, media_type, tmdb_id, tvdb_id, season_number, title),
    )


def fetch_deleted_media(
    conn: sqlite3.Connection, audit_ids: Iterable[int]
) -> dict[int, DeletedMedia]:
    """Return the rows for *audit_ids* that have one, keyed by audit id."""
    ids = list(audit_ids)
    if not ids:
        return {}
    # rationale: placeholders is purely "?" * len(ids) — no value enters the SQL text
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT audit_id, media_type, tmdb_id, tvdb_id, season_number, title "
        f"FROM deleted_media WHERE audit_id IN ({placeholders})",
        ids,
    ).fetchall()
    return {r[0]: DeletedMedia(*r) for r in rows}
