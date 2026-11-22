"""Deletion-lifecycle SQL on ``scheduled_actions`` and re-exports for protection/snooze.

# rationale: protection/snooze reads live in _protection; this module
# owns the deletion-lifecycle mutations (DeletionRow, fetch/mark/delete helpers)
# and re-exports the protection names so callers can import them from here.

``DeletionRow`` and the fetch/mark/delete helpers are kept here because
they form one tight concept: the shape of a row being deleted, how to
retrieve it, and how to advance its state machine. The protection/snooze
read functions are separate because they have no dependency on
``DeletionRow`` and are called from scan-loop hot paths that never need
deletion-state mutations.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import NamedTuple

from mediaman.scanner.repository._protection import (
    cleanup_expired_show_snoozes,
    cleanup_expired_snoozes,
    fetch_already_scheduled_media_ids,
    fetch_kept_show_keys,
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

# Re-export protection names so ``from .scheduled_actions import …``
# call sites resolve.
__all__ = [
    "DELETION_ACTION",
    "ClearedDeletions",
    "DeletionRow",
    "cancel_kept_deletion",
    "claim_due_deletion",
    "claim_due_deletions_under",
    "cleanup_expired_show_snoozes",
    "cleanup_expired_snoozes",
    "clear_pending_deletions",
    "count_pending_deletions",
    "delete_actions_for_media_items",
    "delete_scheduled_action",
    "fetch_already_scheduled_media_ids",
    "fetch_kept_show_keys",
    "fetch_media_held_outside_pass",
    "fetch_pending_deletions",
    "fetch_protected_media_ids",
    "fetch_stuck_deletions",
    "has_expired_snooze",
    "is_already_scheduled",
    "is_claimed_deletion",
    "is_kept_deletion",
    "is_latest_audit",
    "is_protected",
    "is_show_kept",
    "is_show_kept_pure",
    "mark_delete_status",
]


@dataclass(frozen=True, slots=True)
class DeletionRow:
    """A pending or stuck deletion joined with its ``media_items`` row.

    Returned by fetch_pending_deletions and
    fetch_stuck_deletions so the deletion executor consumes typed
    attributes instead of a raw sqlite3.Row. Both queries select
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
    """Return the due deletions, a folder's row before the rows of paths inside it.

    The pass then removes the folder first and closes the rows inside it
    with it; removing an inner path first would leave its item, no longer
    due, holding the folder back. Rows are ordered by normalised path, so
    a spelling with "//" or ".." does not put an inner path first.
    """
    rows = conn.execute(
        "SELECT sa.id, sa.media_item_id, sa.action, mi.file_path, mi.file_size_bytes, "
        "mi.radarr_id, mi.sonarr_id, mi.season_number, mi.title, mi.plex_rating_key "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.action = 'scheduled_deletion' "
        "  AND sa.execute_at <= ? "
        "  AND (sa.delete_status IS NULL OR sa.delete_status = 'pending')",
        (now_iso,),
    ).fetchall()
    due = [_row_to_deletion_row(row) for row in rows]
    return sorted(due, key=lambda row: (_normalised(row.file_path or ""), row.id))


def claim_due_deletion(conn: sqlite3.Connection, action_id: int, now_iso: str) -> bool:
    """Mark a due deletion row ``deleting``; return False if it is no longer due.

    The pass reads its rows once, so a keep, snooze or clear made since then
    must win: the claim only succeeds while the row is still an unused,
    pending ``scheduled_deletion`` whose time has come.
    """
    cursor = conn.execute(
        "UPDATE scheduled_actions SET delete_status = 'deleting' "
        "WHERE id = ? AND action = 'scheduled_deletion' AND token_used = 0 "
        "  AND (delete_status IS NULL OR delete_status = 'pending') "
        "  AND execute_at <= ?",
        (action_id, now_iso),
    )
    return cursor.rowcount == 1


def is_kept_deletion(conn: sqlite3.Connection, action_id: int, now_iso: str) -> bool:
    """Return True if a keep the scan honours covers the item of the deletion row.

    The scan's own rule decides (is_protected, is_show_kept_pure), so the pass
    never removes what a scan would keep. A pure read.
    """
    row = conn.execute(
        "SELECT sa.media_item_id, mi.show_rating_key FROM scheduled_actions sa "
        "LEFT JOIN media_items mi ON sa.media_item_id = mi.id WHERE sa.id = ?",
        (action_id,),
    ).fetchone()
    if row is None:
        return False
    return is_protected(conn, row["media_item_id"]) or is_show_kept_pure(
        conn, row["show_rating_key"], now_iso_str=now_iso
    )


def cancel_kept_deletion(conn: sqlite3.Connection, action_id: int, now_iso: str) -> bool:
    """Delete the deletion row if is_kept_deletion says its item is kept; return True if it did.

    Run it after the claim, inside the claim's transaction, so a keep
    committed before the claim wins.
    """
    if not is_kept_deletion(conn, action_id, now_iso):
        return False
    delete_scheduled_action(conn, action_id)
    return True


def is_claimed_deletion(conn: sqlite3.Connection, action_id: int) -> bool:
    """Return True while the row is still an unused ``scheduled_deletion`` claimed as ``deleting``.

    A keep written onto a claimed row changes its action, and a clear drops it.
    """
    row = conn.execute(
        "SELECT 1 FROM scheduled_actions "
        "WHERE id = ? AND action = 'scheduled_deletion' AND token_used = 0 "
        "  AND delete_status = 'deleting'",
        (action_id,),
    ).fetchone()
    return row is not None


def _normalised(path: str) -> str:
    # normpath keeps a leading "//" (POSIX leaves it implementation-defined).
    return "/" + os.path.normpath(path).lstrip("/") if path.startswith("/") else path


def _at_or_under(stored: str | None, path: str) -> bool:
    """Return True if *stored* names *path* or something under it.

    A match on the stored spelling or on the normalised one counts, so a
    normalised form that hides a match (``..`` after a symlink) never lets
    a path through that the plain spelling would catch.
    """
    if not stored:
        return False
    for candidate, folder in ((stored, path.rstrip("/")), (_normalised(stored), _normalised(path))):
        if candidate == folder or candidate.startswith(folder.rstrip("/") + "/"):
            return True
    return False


def _at_or_under_candidates(path: str) -> tuple[str, tuple[str, ...]]:
    """Return a SQL condition, with its bind values, keeping every row _at_or_under may match.

    _at_or_under still decides; this only spares it reading every row. For
    each spelling of *path*, the range from its bare folder up to that folder
    followed by "0", the character after "/", holds the folder and all under
    it. A stored path holding "//" or "/." may normalise into a match, so it
    is always kept. Comparisons and a fixed GLOB pattern, not LIKE on the
    path, so "%" and "_" in a path are literal.
    """
    raw, norm = path.rstrip("/"), _normalised(path).rstrip("/")
    condition = (
        "((mi.file_path >= ? AND mi.file_path < ?) OR (mi.file_path >= ? AND mi.file_path < ?) "
        "OR mi.file_path GLOB '*/[/.]*')"
    )
    return condition, (raw, raw + "0", norm, norm + "0")


def fetch_media_held_outside_pass(
    conn: sqlite3.Connection, media_item_id: str, path: str, now_iso: str
) -> list[sqlite3.Row]:
    """Return other media items stored at *path* or under it that are not due for deletion.

    Deleting *path* would remove their media too. An item counts as due when
    it has an unused ``scheduled_deletion`` row whose time has come, in any
    delete state, since such rows are part of the same pass.
    """
    where, params = _at_or_under_candidates(path)
    # rationale: the interpolated condition is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT mi.id, mi.title, mi.season_number, mi.file_path FROM media_items mi "
        f"WHERE mi.id != ? AND {where} "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM scheduled_actions sa "
        "    WHERE sa.media_item_id = mi.id AND sa.action = 'scheduled_deletion' "
        "      AND sa.token_used = 0 AND sa.execute_at <= ?"
        "  )",
        (media_item_id, *params, now_iso),
    ).fetchall()
    # Sorted here: ORDER BY in SQL walks the id index and reads every row.
    return sorted(
        (row for row in rows if _at_or_under(row["file_path"], path)), key=lambda row: row["id"]
    )


def claim_due_deletions_under(
    conn: sqlite3.Connection, media_item_id: str, path: str, now_iso: str
) -> list[DeletionRow]:
    """Claim, as claim_due_deletion does, the due rows of other media items at *path* or under it.

    Deleting *path* removes their media too, so their rows must be held by the
    same pass: a keep guarded on the delete state then fails on them.
    Returns the rows claimed.
    """
    where, params = _at_or_under_candidates(path)
    # rationale: the interpolated condition is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT sa.id, sa.media_item_id, sa.action, mi.file_path, mi.file_size_bytes, "
        "mi.radarr_id, mi.sonarr_id, mi.season_number, mi.title, mi.plex_rating_key "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        f"WHERE mi.id != ? AND sa.action = 'scheduled_deletion' AND {where} "
        "ORDER BY sa.id",
        (media_item_id, *params),
    ).fetchall()
    candidates = [_row_to_deletion_row(row) for row in rows]
    return [
        row
        for row in candidates
        if _at_or_under(row.file_path, path) and claim_due_deletion(conn, row.id, now_iso)
    ]


def is_latest_audit(conn: sqlite3.Connection, media_item_id: str, action: str, detail: str) -> bool:
    """Return True if the newest audit entry for *media_item_id* is *action* with *detail*."""
    row = conn.execute(
        "SELECT action, detail FROM audit_log WHERE media_item_id = ? ORDER BY id DESC LIMIT 1",
        (media_item_id,),
    ).fetchone()
    return row is not None and (row["action"], row["detail"]) == (action, detail)


def mark_delete_status(conn: sqlite3.Connection, action_id: int, status: str) -> None:
    """Set ``scheduled_actions.delete_status`` for the given row id."""
    conn.execute(
        "UPDATE scheduled_actions SET delete_status = ? WHERE id = ?",
        (status, action_id),
    )


def delete_scheduled_action(conn: sqlite3.Connection, action_id: int) -> None:
    conn.execute("DELETE FROM scheduled_actions WHERE id = ?", (action_id,))


def delete_actions_for_media_items(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Delete every ``scheduled_actions`` row pointing at *ids*, in chunks.

    Owned by this module because ``scheduled_actions`` is this
    repository's table-group. The scanner's delete phase calls this
    *before* media_items.delete_media_items while holding the
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
        # rationale: IN-clause batching — only "?" placeholders interpolated; every value is bound
        conn.execute(  # nosec B608
            f"DELETE FROM scheduled_actions WHERE media_item_id IN ({placeholders})",
            tuple(chunk),
        )


def count_pending_deletions(conn: sqlite3.Connection) -> int:
    """Return the number of pending ``scheduled_deletion`` rows.

    Counts only rows whose token has not yet been used and whose removal is
    not running now — the rows clear_pending_deletions reports cleared.
    Snoozes and protect-forever rows are not counted.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions "
            "WHERE action='scheduled_deletion' AND token_used=0 "
            "  AND delete_status IS NOT 'deleting'"
        ).fetchone()[0]
    )


class ClearedDeletions(NamedTuple):
    """Rows clear_pending_deletions removed: those waiting, and those already being removed."""

    cleared: int
    already_deleting: int


def clear_pending_deletions(
    conn: sqlite3.Connection,
    *,
    audit_actor: str | None = None,
    audit_ip: str = "",
) -> ClearedDeletions:
    """Delete every pending ``scheduled_deletion`` row in one transaction.

    Snoozes and protect-forever rows are untouched — this only sweeps
    deletions whose token has not yet been used. A row the deletion pass is
    removing right now goes too, so a removal that fails is not retried; it
    is counted apart, since its files may already be gone.

    Audit-in-transaction: when *audit_actor* is supplied, a
    ``sec:scan.cleared`` row is written inside the same
    ``BEGIN IMMEDIATE`` that deletes the rows. The pre-delete counts
    land in the audit detail. If the audit insert raises, the entire
    delete rolls back — we never end up with rows removed but no audit
    trail (the audit insert and the delete are atomic by design; neither
    commits without the other). The waiting count is read by
    count_pending_deletions so the count is independently
    callable.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        cleared = count_pending_deletions(conn)
        already_deleting = int(
            conn.execute(
                "SELECT COUNT(*) FROM scheduled_actions "
                "WHERE action='scheduled_deletion' AND token_used=0 "
                "  AND delete_status = 'deleting'"
            ).fetchone()[0]
        )
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
                detail={"count": cleared, "already_deleting": already_deleting},
            )
    return ClearedDeletions(cleared, already_deleting)
