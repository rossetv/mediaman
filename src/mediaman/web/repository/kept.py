"""Repository functions for kept/protected-item queries.

Covers reads and writes against scheduled_actions, media_items, and kept_shows
that are specific to the kept/protected web routes. Scanner-facing mutations
live in mediaman.scanner.repository.scheduled_actions.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from mediaman.core.time import now_iso
from mediaman.scanner.repository import (
    book_removed_files,
    book_removed_files_where,
    fetch_protected_media_ids,
    is_show_kept_pure,
)
from mediaman.web.repository.library_query import ALL_SEASON_TYPES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProtectedRow:
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
class KeepRow:
    action: str
    execute_at: str | None
    kept_at: str | None = None


@dataclass(frozen=True)
class SeasonRow:
    id: str
    season_number: int | None
    title: str
    kept: bool
    keep: KeepRow | None
    file_size_bytes: int
    last_watched_at: str | None


def resolve_show_rating_key(
    conn: sqlite3.Connection, supplied_key: str
) -> tuple[str | None, str | None]:
    """Return ``(resolved_key, error)`` for a keep-show request.

    IDOR risk closed by this helper: it never falls back to matching seasons
    by show_title when the supplied rating key is missing on the stored
    rows.  Two distinct shows sharing a title (a common case — remakes,
    international versions) would collide, so user A keeping *Kingdom*
    would also match user B's *Kingdom* rows.

    Resolution rules:
      (a) supplied_key is present and at least one media_items row carries
          that exact show_rating_key — use the supplied key.
      (b) anything else — return ``(None, error_message)`` so the caller
          can 409.

    ``supplied_key`` is the raw path parameter.  Callers pass it through
    unchanged — never synthesised from show_title.
    """
    key = (supplied_key or "").strip()
    if key:
        if show_rating_key_exists(conn, key):
            return key, None
        return None, "Unknown show_rating_key"
    return None, "show_rating_key required"


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

    Matches the rows the Library groups into one show row: the key is
    ``COALESCE(show_rating_key, show_title)``, so a show Plex gave no rating
    key is found by its title.

    A season is kept by the scanner's rule (see ``fetch_protected_media_ids``),
    read for all seasons in one batched query.
    """
    # rationale: placeholders is purely "?" * len(ALL_SEASON_TYPES) — no user value ever enters the SQL text
    type_placeholders = ",".join("?" * len(ALL_SEASON_TYPES))
    rows = conn.execute(
        "SELECT id, title, season_number, file_size_bytes, last_watched_at "
        "FROM media_items "
        "WHERE COALESCE(show_rating_key, show_title) = ? "
        f"AND media_type IN ({type_placeholders}) "
        "ORDER BY season_number ASC",
        (show_rating_key, *ALL_SEASON_TYPES),
    ).fetchall()

    now = now_iso()
    kept_set = fetch_protected_media_ids(conn, [r["id"] for r in rows], now)
    keeps = _longest_keeps(conn, sorted(kept_set), now)

    return [
        SeasonRow(
            id=r["id"],
            season_number=r["season_number"],
            title=r["title"],
            kept=r["id"] in kept_set,
            keep=keeps.get(r["id"]),
            file_size_bytes=r["file_size_bytes"] or 0,
            last_watched_at=r["last_watched_at"],
        )
        for r in rows
    ]


def _longest_keeps(conn: sqlite3.Connection, media_ids: list[str], now: str) -> dict[str, KeepRow]:
    """The keep in force of each of *media_ids* that has one: of several, the longest."""
    keeps: dict[str, KeepRow] = {}
    # rationale: placeholders is purely "?" * len(media_ids); every value is bound
    placeholders = ",".join("?" * len(media_ids))
    for r in conn.execute(
        "SELECT media_item_id, action, execute_at, snoozed_at FROM scheduled_actions "
        f"WHERE media_item_id IN ({placeholders}) AND (action = 'protected_forever' "
        "OR (action = 'snoozed' AND execute_at >= ?)) "
        "ORDER BY action = 'protected_forever', execute_at",
        (*media_ids, now),
    ).fetchall():
        keeps[r["media_item_id"]] = KeepRow(r["action"], r["execute_at"], r["snoozed_at"])
    return keeps


def fetch_keep(conn: sqlite3.Connection, media_id: str) -> KeepRow | None:
    """The keep in force of one item: of several, the longest."""
    return _longest_keeps(conn, [media_id], now_iso()).get(media_id)


def fetch_show_kept_status(conn: sqlite3.Connection, show_rating_key: str) -> KeepRow | None:
    """The show's keep in force; a lapsed one is none, though its row waits for the next scan."""
    row = conn.execute(
        "SELECT action, execute_at, created_at FROM kept_shows WHERE show_rating_key = ?",
        (show_rating_key,),
    ).fetchone()
    if row is None or not is_show_kept_pure(conn, show_rating_key):
        return None
    return KeepRow(row["action"], row["execute_at"], row["created_at"])


def same_keep(read: KeepRow | None, keep: KeepRow | None) -> bool:
    """True when *keep* is still what a person read; when it was made counts where they say it."""
    if read is None or keep is None:
        return read is keep
    return (read.action, read.execute_at) == (keep.action, keep.execute_at) and (
        read.kept_at is None or read.kept_at == keep.kept_at
    )


def fetch_existing_actions_for_seasons(
    conn: sqlite3.Connection, season_ids: list[str]
) -> dict[str, list[int]]:
    """Return {media_item_id: [scheduled_actions.id, ...]} for each season's keep rows.

    Every scheduled-deletion, snooze or keep-forever row counts, whether or not its
    email token was used. A row the deletion pass is working on comes first, then the
    newest: the first id is the row a new keep is written onto, the rest are replaced.
    Uses a batched IN-clause to avoid N+1 queries.
    """
    if not season_ids:
        return {}
    # rationale: placeholders is purely "?" * len(season_ids) — no user value ever enters the SQL text
    placeholders = ",".join("?" * len(season_ids))
    existing_rows = conn.execute(
        f"SELECT id, media_item_id FROM scheduled_actions "
        f"WHERE media_item_id IN ({placeholders}) "
        f"AND action IN ('scheduled_deletion', 'snoozed', 'protected_forever') "
        f"ORDER BY delete_status IS NOT 'deleting', id DESC",
        season_ids,
    ).fetchall()
    by_season: dict[str, list[int]] = {}
    for er in existing_rows:
        by_season.setdefault(str(er["media_item_id"]), []).append(er["id"])
    return by_season


def fetch_show_keep_row(conn: sqlite3.Connection, show_rating_key: str) -> tuple[int, str] | None:
    row = conn.execute(
        "SELECT id, show_title FROM kept_shows WHERE show_rating_key = ?",
        (show_rating_key,),
    ).fetchone()
    if row is None:
        return None
    return row["id"], row["show_title"]


def show_rating_key_exists(conn: sqlite3.Connection, show_rating_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM media_items WHERE show_rating_key = ? LIMIT 1",
        (show_rating_key,),
    ).fetchone()
    return row is not None


def fetch_show_title(conn: sqlite3.Connection, show_rating_key: str) -> str | None:
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
    if not season_ids:
        return set()
    # rationale: placeholders is purely "?" * len(season_ids) — no user value ever enters the SQL text
    placeholders = ",".join("?" * len(season_ids))
    rows = conn.execute(
        f"SELECT id FROM media_items WHERE id IN ({placeholders}) AND show_rating_key = ?",
        (*tuple(season_ids), show_rating_key),
    ).fetchall()
    return {r["id"] for r in rows}


def fetch_unkeyed_media_ids(conn: sqlite3.Connection, candidate_ids: set[str]) -> list[str]:
    """Return the subset of ``candidate_ids`` whose media_items row has NULL/empty show_rating_key.

    Used by the keep-show flow to surface a diagnostic warning when the
    supplied seasons carry no show key and so cannot be matched to a show.
    """
    if not candidate_ids:
        return []
    # rationale: placeholders is purely "?" * len(candidate_ids) — no user value ever enters the SQL text
    placeholders = ",".join("?" * len(candidate_ids))
    rows = conn.execute(
        f"SELECT id FROM media_items WHERE id IN ({placeholders}) "
        f"AND (show_rating_key IS NULL OR show_rating_key = '')",
        tuple(candidate_ids),
    ).fetchall()
    return [r["id"] for r in rows]


def delete_protections(conn: sqlite3.Connection, media_item_id: str) -> int:
    """Delete every keep (forever or snoozed) on the item and return how many went.

    Scheduled-deletion rows, and any row the deletion pass is working on, are left alone.
    What a keep's row records as removed is booked first.
    """
    where = (
        "media_item_id = ? AND action IN ('protected_forever', 'snoozed') "
        "AND delete_status IS NOT 'deleting'"
    )
    book_removed_files_where(conn, where, (media_item_id,))
    # rationale: the interpolated condition is a fixed string; every value is bound
    return conn.execute(  # nosec B608
        f"DELETE FROM scheduled_actions WHERE {where}", (media_item_id,)
    ).rowcount


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
    conn.execute(
        "INSERT INTO kept_shows "
        "(show_rating_key, show_title, action, execute_at, snooze_duration, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(show_rating_key) DO UPDATE SET action=excluded.action, "
        "execute_at=excluded.execute_at, snooze_duration=excluded.snooze_duration, "
        "created_at=excluded.created_at",
        (show_rating_key, show_title, action, execute_at, snooze_duration, created_at),
    )


def delete_kept_show(conn: sqlite3.Connection, kept_show_id: int) -> None:
    conn.execute("DELETE FROM kept_shows WHERE id = ?", (kept_show_id,))


def set_protected_state(
    conn: sqlite3.Connection,
    *,
    to_update: Sequence[tuple[object, ...]],
    to_insert: Sequence[tuple[object, ...]],
    to_delete: Sequence[int] = (),
) -> bool:
    """Apply batched updates, inserts and deletes to scheduled_actions for season keeps.

    ``to_delete`` ids are the older rows a keep replaces; one the deletion pass is
    working on is never deleted. What the rows record as removed is booked first.

    ``to_update`` rows: (action, execute_at, snoozed_at, snooze_duration, id)
    ``to_insert`` rows: (media_item_id, action, scheduled_at, execute_at,
                         token, created_at, snooze_duration)

    Returns True when a row it updated is being removed by the deletion pass
    right now. The keep is still written: if that removal fails, the season
    stays kept.
    """
    deletion_running = False
    book_removed_files(conn, [row[4] for row in to_update] + list(to_delete))
    if to_update:
        ids = [row[4] for row in to_update]
        # rationale: placeholders is purely "?" * len(ids) — no user value ever enters the SQL text
        placeholders = ",".join("?" * len(ids))
        running = conn.execute(
            f"SELECT 1 FROM scheduled_actions "
            f"WHERE id IN ({placeholders}) AND delete_status = 'deleting' LIMIT 1",
            ids,
        ).fetchone()
        deletion_running = running is not None
        conn.executemany(
            "UPDATE scheduled_actions SET action = ?, execute_at = ?, "
            "snoozed_at = ?, snooze_duration = ?, token_used = 0 WHERE id = ?",
            to_update,
        )
    if to_delete:
        conn.executemany(
            "DELETE FROM scheduled_actions WHERE id = ? AND delete_status IS NOT 'deleting'",
            [(row_id,) for row_id in to_delete],
        )
    if to_insert:
        conn.executemany(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, execute_at, token, token_used, "
            "snoozed_at, snooze_duration) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            to_insert,
        )
    return deletion_running
