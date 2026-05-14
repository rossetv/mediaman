"""Deletion-lifecycle SQL on ``scheduled_actions`` and re-exports for protection/snooze.

# rationale: protection/snooze reads live in :mod:`_protection`; this module
# owns the deletion-lifecycle mutations (DeletionRow, fetch/mark/delete helpers)
# and re-exports the protection names so callers that import from
# :mod:`scheduled_actions` continue to work unmodified.

``DeletionRow`` and the fetch/mark/delete helpers are kept here because
they form one tight concept: the shape of a row being deleted, how to
retrieve it, and how to advance its state machine. The protection/snooze
read functions are separate because they have no dependency on
``DeletionRow`` and are called from scan-loop hot paths that never need
deletion-state mutations.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from mediaman.scanner.repository._protection import (
    cleanup_expired_show_snoozes,
    cleanup_expired_snoozes,
    fetch_already_scheduled_media_ids,
    fetch_protected_media_ids,
    has_expired_snooze,
    is_already_scheduled,
    is_protected,
    is_show_kept,
    is_show_kept_pure,
)

logger = logging.getLogger(__name__)

# The action that means deletion is already lined up.
DELETION_ACTION = "scheduled_deletion"

# Re-export protection names so existing ``from .scheduled_actions import …``
# call sites continue to resolve without change.
__all__ = [
    "DELETION_ACTION",
    "DeletionRow",
    "cleanup_expired_show_snoozes",
    "cleanup_expired_snoozes",
    "clear_pending_deletions",
    "count_pending_deletions",
    "delete_actions_for_media_items",
    "delete_scheduled_action",
    "fetch_already_scheduled_media_ids",
    "fetch_pending_deletions",
    "fetch_protected_media_ids",
    "fetch_stuck_deletions",
    "has_expired_snooze",
    "is_already_scheduled",
    "is_protected",
    "is_show_kept",
    "is_show_kept_pure",
    "mark_delete_status",
]


@dataclass(frozen=True, slots=True)
class DeletionRow:
    """A pending or stuck deletion joined with its ``media_items`` row.

    Returned by :func:`fetch_pending_deletions` and
    :func:`fetch_stuck_deletions` so the deletion executor consumes typed
    attributes instead of a raw :class:`sqlite3.Row`. Both queries select
    the same column set; columns a given query does not logically need
    (``action`` for the pending path, the *arr ids for the stuck path)
    are still selected so the dataclass shape is uniform — the executor
    simply does not read them on that path.
    """

    id: int
    media_item_id: str
    action: str | None
    file_path: str | None
    file_size_bytes: int | None
    title: str | None
    plex_rating_key: str | None
    radarr_id: int | None
    sonarr_id: int | None
    season_number: int | None


def _row_to_deletion_row(row: sqlite3.Row) -> DeletionRow:
    """Map a joined ``scheduled_actions``/``media_items`` row to a :class:`DeletionRow`."""
    return DeletionRow(
        id=row["id"],
        media_item_id=row["media_item_id"],
        action=row["action"],
        file_path=row["file_path"],
        file_size_bytes=row["file_size_bytes"],
        title=row["title"],
        plex_rating_key=row["plex_rating_key"],
        radarr_id=row["radarr_id"],
        sonarr_id=row["sonarr_id"],
        season_number=row["season_number"],
    )


# ---------------------------------------------------------------------------
# Deletion lifecycle — fetch, mark, delete
# ---------------------------------------------------------------------------


def fetch_stuck_deletions(conn: sqlite3.Connection) -> list[DeletionRow]:
    """Return rows in ``scheduled_actions`` still marked ``deleting``.

    Returns an empty list if the ``delete_status`` column has not been
    migrated yet (older DB schemas).
    """
    try:
        rows = conn.execute(
            "SELECT sa.id, sa.media_item_id, sa.action, mi.file_path, "
            "mi.file_size_bytes, mi.title, mi.plex_rating_key, "
            "mi.radarr_id, mi.sonarr_id, mi.season_number "
            "FROM scheduled_actions sa "
            "LEFT JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.delete_status = 'deleting'"
        ).fetchall()
    except sqlite3.OperationalError:
        # delete_status column not yet migrated — nothing to do.
        return []
    return [_row_to_deletion_row(row) for row in rows]


def fetch_pending_deletions(conn: sqlite3.Connection, now_iso: str) -> list[DeletionRow]:
    """Return all pending deletions whose grace period has elapsed."""
    rows = conn.execute(
        "SELECT sa.id, sa.media_item_id, sa.action, mi.file_path, mi.file_size_bytes, "
        "mi.radarr_id, mi.sonarr_id, mi.season_number, mi.title, mi.plex_rating_key "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.action = 'scheduled_deletion' "
        "  AND sa.execute_at < ? "
        "  AND (sa.delete_status IS NULL OR sa.delete_status = 'pending')",
        (now_iso,),
    ).fetchall()
    return [_row_to_deletion_row(row) for row in rows]


def mark_delete_status(conn: sqlite3.Connection, action_id: int, status: str) -> None:
    """Set ``scheduled_actions.delete_status`` for the given row id."""
    conn.execute(
        "UPDATE scheduled_actions SET delete_status = ? WHERE id = ?",
        (status, action_id),
    )


def delete_scheduled_action(conn: sqlite3.Connection, action_id: int) -> None:
    """Remove a row from ``scheduled_actions``."""
    conn.execute("DELETE FROM scheduled_actions WHERE id = ?", (action_id,))


def delete_actions_for_media_items(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Delete every ``scheduled_actions`` row pointing at *ids*, in chunks.

    Owned by this module because ``scheduled_actions`` is this
    repository's table-group. The scanner's delete phase calls this
    *before* :func:`media_items.delete_media_items` while holding the
    same transaction at the call site, so the two DELETEs stay atomic
    and a crash between them cannot orphan a ``scheduled_actions`` row
    against a deleted ``media_items`` row. This function therefore opens
    no transaction of its own.
    """
    if not ids:
        return
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        # rationale: §9.6 IN-clause batching — only "?" placeholders interpolated; every value is bound
        conn.execute(  # nosec B608
            f"DELETE FROM scheduled_actions WHERE media_item_id IN ({placeholders})",
            tuple(chunk),
        )


def count_pending_deletions(conn: sqlite3.Connection) -> int:
    """Return the number of pending ``scheduled_deletion`` rows.

    Counts only rows whose token has not yet been used — the same set
    :func:`clear_pending_deletions` sweeps. Snoozes and protect-forever
    rows are not counted.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions "
            "WHERE action='scheduled_deletion' AND token_used=0"
        ).fetchone()[0]
    )


def clear_pending_deletions(
    conn: sqlite3.Connection,
    *,
    audit_actor: str | None = None,
    audit_ip: str = "",
) -> int:
    """Delete every pending ``scheduled_deletion`` row in one transaction.

    Returns the number of rows removed so callers can surface the count
    in their HTTP response. Snoozes and protect-forever rows are
    untouched — this only sweeps pending deletions whose token has not
    yet been used.

    Audit-in-transaction: when *audit_actor* is supplied, a
    ``sec:scan.cleared`` row is written inside the same
    ``BEGIN IMMEDIATE`` that deletes the rows. The pre-delete count
    lands in the audit detail. If the audit insert raises, the entire
    delete rolls back — we never end up with rows removed but no audit
    trail (the audit insert and the delete are atomic by design; neither
    commits without the other). The count read is delegated to
    :func:`count_pending_deletions` so the count is independently
    callable.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        cleared = count_pending_deletions(conn)
        conn.execute(
            "DELETE FROM scheduled_actions WHERE action='scheduled_deletion' AND token_used=0"
        )
        if audit_actor is not None:
            from mediaman.core.audit import security_event_or_raise

            security_event_or_raise(
                conn,
                event="scan.cleared",
                actor=audit_actor,
                ip=audit_ip,
                detail={"count": cleared},
            )
    return cleared
