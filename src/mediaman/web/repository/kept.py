"""Repository functions for kept/protected-item queries.

Covers reads and writes against scheduled_actions, media_items, and kept_shows
that are specific to the kept/protected web routes. Scanner-facing mutations
live in mediaman.scanner.repository.scheduled_actions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True)
class ProtectedRow:
    """A single scheduled_actions row joined with media_items for the kept page."""

    sa_id: int
    media_item_id: str
    action: str
    execute_at: str | None
    snooze_duration: str
    title: str
    media_type: str
    show_title: str | None
    season_number: int | None
    plex_rating_key: str | None
    file_size_bytes: int


@dataclass(frozen=True)
class SeasonRow:
    """A media_items row with a flag indicating whether the season is kept."""

    id: str
    season_number: int | None
    title: str
    kept: bool
    file_size_bytes: int
    last_watched_at: str | None


@dataclass(frozen=True)
class ShowKeptRow:
    """A kept_shows row for a specific show."""

    action: str
    execute_at: str | None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def fetch_protected_items(conn: sqlite3.Connection, now: str) -> list[ProtectedRow]:
    """Return all actively protected / snoozed scheduled_actions rows.

    Returns rows for both protected_forever items and snoozed items whose
    execute_at is still in the future.  Ordered so protected_forever comes
    before snoozed (action DESC) and then by execute_at ASC within each group.
    """
    rows = conn.execute(
        """
        SELECT
            sa.id          AS sa_id,
            sa.media_item_id,
            sa.action,
            sa.execute_at,
            sa.snooze_duration,
            mi.title,
            mi.media_type,
            mi.show_title,
            mi.season_number,
            mi.plex_rating_key,
            mi.file_size_bytes
        FROM scheduled_actions sa
        JOIN media_items mi ON mi.id = sa.media_item_id
        WHERE sa.action = 'protected_forever'
           OR (sa.action = 'snoozed' AND sa.execute_at > ?)
        ORDER BY sa.action DESC, sa.execute_at ASC
        """,
        (now,),
    ).fetchall()

    return [
        ProtectedRow(
            sa_id=r["sa_id"],
            media_item_id=r["media_item_id"],
            action=r["action"],
            execute_at=r["execute_at"],
            snooze_duration=r["snooze_duration"] or "",
            title=r["title"],
            media_type=r["media_type"] or "movie",
            show_title=r["show_title"],
            season_number=r["season_number"],
            plex_rating_key=r["plex_rating_key"],
            file_size_bytes=r["file_size_bytes"] or 0,
        )
        for r in rows
    ]


def fetch_seasons_for_show(conn: sqlite3.Connection, show_rating_key: str) -> list[SeasonRow]:
    """Return all seasons of a show ordered by season_number.

    Performs the batched IN-clause approach to fetch kept status for all
    seasons in one extra query instead of N+1 per season.
    """
    rows = conn.execute(
        "SELECT id, title, season_number, file_size_bytes, last_watched_at "
        "FROM media_items "
        "WHERE show_rating_key = ? ORDER BY season_number ASC",
        (show_rating_key,),
    ).fetchall()

    ids = [r["id"] for r in rows]
    kept_set: set[str] = set()
    if ids:
        placeholders = ",".join("?" * len(ids))
        kept_rows = conn.execute(
            f"SELECT media_item_id FROM scheduled_actions "
            f"WHERE media_item_id IN ({placeholders}) "
            "AND action IN ('protected_forever', 'snoozed') AND token_used = 0",
            ids,
        ).fetchall()
        kept_set = {str(kr["media_item_id"]) for kr in kept_rows}

    return [
        SeasonRow(
            id=r["id"],
            season_number=r["season_number"],
            title=r["title"],
            kept=r["id"] in kept_set,
            file_size_bytes=r["file_size_bytes"] or 0,
            last_watched_at=r["last_watched_at"],
        )
        for r in rows
    ]


def fetch_show_kept_status(conn: sqlite3.Connection, show_rating_key: str) -> ShowKeptRow | None:
    """Return the kept_shows row for a show, or None if not kept."""
    row = conn.execute(
        "SELECT action, execute_at FROM kept_shows WHERE show_rating_key = ?",
        (show_rating_key,),
    ).fetchone()
    if row is None:
        return None
    return ShowKeptRow(action=row["action"], execute_at=row["execute_at"])


def fetch_active_protection(conn: sqlite3.Connection, media_item_id: str) -> int | None:
    """Return the id of the most-recent active protection row, or None.

    Picks the most-recent protection row to avoid targeting a stale snooze
    when a newer protect/snooze has already been applied.
    """
    row = conn.execute(
        "SELECT id FROM scheduled_actions "
        "WHERE media_item_id = ? AND action IN ('protected_forever', 'snoozed') "
        "ORDER BY id DESC LIMIT 1",
        (media_item_id,),
    ).fetchone()
    return row["id"] if row is not None else None


def fetch_existing_actions_for_seasons(
    conn: sqlite3.Connection, season_ids: list[str]
) -> dict[str, int]:
    """Return {media_item_id: scheduled_actions.id} for existing (untriggered) rows.

    Uses a batched IN-clause to avoid N+1 queries.
    """
    if not season_ids:
        return {}
    placeholders = ",".join("?" * len(season_ids))
    existing_rows = conn.execute(
        f"SELECT id, media_item_id FROM scheduled_actions "
        f"WHERE media_item_id IN ({placeholders}) AND token_used = 0",
        season_ids,
    ).fetchall()
    return {str(er["media_item_id"]): er["id"] for er in existing_rows}


def fetch_show_keep_row(conn: sqlite3.Connection, show_rating_key: str) -> tuple[int, str] | None:
    """Return (id, show_title) from kept_shows for the given key, or None."""
    row = conn.execute(
        "SELECT id, show_title FROM kept_shows WHERE show_rating_key = ?",
        (show_rating_key,),
    ).fetchone()
    if row is None:
        return None
    return row["id"], row["show_title"]


def show_rating_key_exists(conn: sqlite3.Connection, show_rating_key: str) -> bool:
    """Return True if any media_items row carries the given show_rating_key."""
    row = conn.execute(
        "SELECT 1 FROM media_items WHERE show_rating_key = ? LIMIT 1",
        (show_rating_key,),
    ).fetchone()
    return row is not None


def fetch_show_title(conn: sqlite3.Connection, show_rating_key: str) -> str | None:
    """Return the show_title for the first media_items row with the given key, or None."""
    row = conn.execute(
        "SELECT show_title FROM media_items WHERE show_rating_key = ? LIMIT 1",
        (show_rating_key,),
    ).fetchone()
    if row is None:
        return None
    return cast(str, row["show_title"])


def fetch_owned_season_ids(
    conn: sqlite3.Connection, season_ids: list[str], show_rating_key: str
) -> set[str]:
    """Return the subset of ``season_ids`` whose media_items row is owned by ``show_rating_key``."""
    if not season_ids:
        return set()
    placeholders = ",".join("?" * len(season_ids))
    rows = conn.execute(
        f"SELECT id FROM media_items WHERE id IN ({placeholders}) AND show_rating_key = ?",
        (*tuple(season_ids), show_rating_key),
    ).fetchall()
    return {r["id"] for r in rows}


def fetch_unkeyed_media_ids(conn: sqlite3.Connection, candidate_ids: set[str]) -> list[str]:
    """Return the subset of ``candidate_ids`` whose media_items row has NULL/empty show_rating_key.

    Used by the keep-show flow to surface a diagnostic warning when the
    supplied seasons would have triggered the (removed) show_title
    fallback.
    """
    if not candidate_ids:
        return []
    placeholders = ",".join("?" * len(candidate_ids))
    rows = conn.execute(
        f"SELECT id FROM media_items WHERE id IN ({placeholders}) "
        f"AND (show_rating_key IS NULL OR show_rating_key = '')",
        tuple(candidate_ids),
    ).fetchall()
    return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def delete_protection(conn: sqlite3.Connection, action_id: int) -> None:
    """Delete a single scheduled_actions row by primary key."""
    conn.execute("DELETE FROM scheduled_actions WHERE id = ?", (action_id,))


def upsert_kept_show(
    conn: sqlite3.Connection,
    *,
    show_rating_key: str,
    show_title: str,
    action: str,
    execute_at: str | None,
    snooze_duration: str,
    created_at: str,
) -> None:
    """Insert or update a kept_shows row."""
    conn.execute(
        "INSERT INTO kept_shows "
        "(show_rating_key, show_title, action, execute_at, snooze_duration, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(show_rating_key) DO UPDATE SET action=excluded.action, "
        "execute_at=excluded.execute_at, snooze_duration=excluded.snooze_duration",
        (show_rating_key, show_title, action, execute_at, snooze_duration, created_at),
    )


def delete_kept_show(conn: sqlite3.Connection, kept_show_id: int) -> None:
    """Delete a kept_shows row by primary key."""
    conn.execute("DELETE FROM kept_shows WHERE id = ?", (kept_show_id,))


def set_protected_state(
    conn: sqlite3.Connection,
    *,
    to_update: Sequence[tuple[object, ...]],
    to_insert: Sequence[tuple[object, ...]],
) -> None:
    """Apply batched updates and inserts to scheduled_actions for season keeps.

    ``to_update`` rows: (action, execute_at, snoozed_at, snooze_duration, id)
    ``to_insert`` rows: (media_item_id, action, scheduled_at, execute_at,
                         token, created_at, snooze_duration)
    """
    if to_update:
        conn.executemany(
            "UPDATE scheduled_actions SET action = ?, execute_at = ?, "
            "snoozed_at = ?, snooze_duration = ? WHERE id = ?",
            to_update,
        )
    if to_insert:
        conn.executemany(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, execute_at, token, token_used, "
            "snoozed_at, snooze_duration) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            to_insert,
        )
