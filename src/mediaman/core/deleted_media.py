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


def redownload_tokens(stored: DeletedMedia | None) -> list[str]:
    """The audit ids a re-download of the deleted item is recorded under.

    A movie by ``tmdb:<id>``; anything else by ``tvdb:<id>`` (the form recorded before
    seasons were tracked) and ``tvdb:<id>:s<n>``.
    """
    if stored is None:
        return []
    if stored.media_type == "movie":
        return [f"tmdb:{stored.tmdb_id}"] if stored.tmdb_id else []
    if not stored.tvdb_id:
        return []
    tokens = [f"tvdb:{stored.tvdb_id}"]
    if stored.season_number is not None:
        tokens.append(f"tvdb:{stored.tvdb_id}:s{stored.season_number}")
    return tokens


def was_redownloaded_after(
    deletion_created_at: str, keys: Iterable[str], redownload_times: dict[str, str]
) -> bool:
    """Whether any of *keys* has a re-download newer than the deletion."""
    last = max((redownload_times[k] for k in keys if k in redownload_times), default=None)
    return bool(last and last > deletion_created_at)
