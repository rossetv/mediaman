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

import json
import logging
import os
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from mediaman.core.audit import log_audit
from mediaman.core.deleted_media import DeletedMedia, record_deleted_media, redownload_tokens
from mediaman.core.library_selected import LIBRARY_SELECTED
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
    restore_keeps,
    save_pruned_keeps,
    take_pruned_keeps,
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
    "await_rescan",
    "book_deletion",
    "book_removed_files",
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
    "fetch_deletion",
    "fetch_kept_show_keys",
    "fetch_media_held_outside_pass",
    "fetch_pending_deletions",
    "fetch_pending_rescans",
    "fetch_pending_unmonitors",
    "fetch_protected_media_ids",
    "fetch_stuck_deletions",
    "fetch_tracked_paths_in_folders",
    "fetch_unbooked_media_ids",
    "finish_unmonitor",
    "forget_unremoved_files",
    "has_expired_snooze",
    "has_pending_unmonitors",
    "is_already_scheduled",
    "is_claimed_deletion",
    "is_fully_removed",
    "is_kept_deletion",
    "is_latest_audit",
    "is_protected",
    "is_show_kept",
    "is_show_kept_pure",
    "mark_delete_status",
    "record_removed_files",
    "restore_keeps",
    "save_pruned_keeps",
    "take_pruned_keeps",
    "tracked_paths",
    "was_audited_since",
    "was_requested_since",
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
    media_type: str | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    # ``{"path", "size"}`` entries; None until a scan records the item's files.
    tracked_files: list[dict[str, Any]] | None = None
    # Tracked paths earlier passes of this deletion removed, and the bytes they freed.
    removed_paths: frozenset[str] = frozenset()
    removed_bytes: int = 0
    # The tracked size of each removed path; a record written without one has none.
    removed_sizes: dict[str, int] = field(default_factory=dict)
    # Tracked paths the last pass left to remove; None when its record does not say.
    left_paths: frozenset[str] | None = None
    # Whether the item's Plex library is ticked in Settings.
    library_selected: bool = True


def tracked_paths(row: DeletionRow) -> list[str] | None:
    """Return the paths of the row's tracked files, or None when they are not usable.

    None when the scan has not recorded them yet, or an entry has no path.
    """
    if not row.tracked_files:
        return None
    paths = [entry.get("path") if isinstance(entry, dict) else None for entry in row.tracked_files]
    if not all(isinstance(path, str) and path for path in paths):
        return None
    return [str(path) for path in paths]


def is_fully_removed(row: DeletionRow) -> bool:
    """Return True when this deletion has removed every file it had to.

    With no files tracked any more, what the last pass left decides.
    """
    files = tracked_paths(row)
    remaining = row.left_paths or () if files is None else {str(Path(path)) for path in files}
    return bool(row.removed_paths) and row.removed_paths.issuperset(remaining)


_DELETION_COLUMNS = (
    "sa.id, sa.media_item_id, sa.action, mi.file_path, mi.file_size_bytes, "
    "mi.title, mi.plex_rating_key, mi.radarr_id, mi.sonarr_id, mi.season_number, "
    "mi.media_type, mi.tmdb_id, mi.tvdb_id, mi.tracked_files, sa.removed_files, "
    f"{LIBRARY_SELECTED} AS library_selected"
)


def _parse_tracked_files(raw: str | None) -> list[dict[str, Any]] | None:
    """Return the stored file list, or None when it is missing or not a JSON list."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("media_items.tracked_files is not valid JSON; treating it as unrecorded")
        return None
    return value if isinstance(value, list) else None


@dataclass(frozen=True, slots=True)
class _RemovedFiles:
    paths: frozenset[str] = frozenset()
    removed_bytes: int = 0
    sizes: dict[str, int] = field(default_factory=dict)
    left: frozenset[str] | None = None


def _parse_removed_files(raw: str | None) -> _RemovedFiles:
    """Return the record of removed files, or an empty one when missing or malformed."""
    if raw is None:
        return _RemovedFiles()
    try:
        value = json.loads(raw)
        paths = value["paths"]
        removed_bytes = value["bytes"]
    except (TypeError, ValueError, KeyError):
        logger.warning("scheduled_actions.removed_files is malformed; treating it as empty")
        return _RemovedFiles()
    if not isinstance(paths, list) or not isinstance(removed_bytes, int):
        return _RemovedFiles()
    removed = frozenset(p for p in paths if isinstance(p, str))
    sizes = value.get("sizes")
    if not isinstance(sizes, dict):
        sizes = {}
    left = value.get("left")
    return _RemovedFiles(
        removed,
        removed_bytes,
        {
            path: size
            for path, size in sizes.items()
            if path in removed and isinstance(size, int) and not isinstance(size, bool)
        },
        frozenset(p for p in left if isinstance(p, str)) if isinstance(left, list) else None,
    )


def _removed_json(
    paths: Collection[str], removed_bytes: int, sizes: Mapping[str, int], left: Collection[str]
) -> str:
    return json.dumps(
        {
            "paths": sorted(paths),
            "bytes": removed_bytes,
            "sizes": {path: size for path, size in sorted(sizes.items()) if path in paths},
            "left": sorted(left),
        }
    )


def record_removed_files(
    conn: sqlite3.Connection,
    action_id: int,
    paths: Collection[str],
    removed_bytes: int,
    sizes: Mapping[str, int],
    left: Collection[str],
) -> bool:
    """Store what this deletion has removed so far, so a later pass can finish it.

    *sizes* gives the tracked size of each removed path that has one; *left*
    holds the tracked paths still to remove. Returns False when the row is gone.
    """
    cursor = conn.execute(
        "UPDATE scheduled_actions SET removed_files = ? WHERE id = ?",
        (_removed_json(paths, removed_bytes, sizes, left), action_id),
    )
    return cursor.rowcount == 1


def _row_to_deletion_row(row: sqlite3.Row) -> DeletionRow:
    removed = _parse_removed_files(row["removed_files"])
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
        media_type=row["media_type"],
        tmdb_id=row["tmdb_id"],
        tvdb_id=row["tvdb_id"],
        tracked_files=_parse_tracked_files(row["tracked_files"]),
        removed_paths=removed.paths,
        removed_bytes=removed.removed_bytes,
        removed_sizes=removed.sizes,
        left_paths=removed.left,
        library_selected=bool(row["library_selected"]),
    )


def fetch_stuck_deletions(conn: sqlite3.Connection) -> list[DeletionRow]:
    """Return rows in ``scheduled_actions`` still marked ``deleting``.

    Returns an empty list if the ``delete_status`` column has not been
    migrated yet (older DB schemas).
    """
    try:
        # rationale: the interpolated column list is a fixed string
        rows = conn.execute(  # nosec B608
            f"SELECT {_DELETION_COLUMNS} "
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
    # rationale: the interpolated column list is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT {_DELETION_COLUMNS} "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.action = 'scheduled_deletion' "
        "  AND sa.execute_at <= ? "
        "  AND (sa.delete_status IS NULL OR sa.delete_status = 'pending')",
        (now_iso,),
    ).fetchall()
    due = [_row_to_deletion_row(row) for row in rows]
    return sorted(due, key=lambda row: (_normalised(row.file_path or ""), row.id))


def fetch_deletion(conn: sqlite3.Connection, action_id: int) -> DeletionRow | None:
    """Return the deletion row as it is now, or None when it is gone."""
    # rationale: the interpolated column list is a fixed string; every value is bound
    row = conn.execute(  # nosec B608
        f"SELECT {_DELETION_COLUMNS} "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.id = ?",
        (action_id,),
    ).fetchone()
    return _row_to_deletion_row(row) if row else None


def _fetch_unbooked(conn: sqlite3.Connection) -> list[tuple[DeletionRow, bool]]:
    """Return each live deletion with a record of removed files, and whether it is claimed.

    Those a keep was written over are included: their removal is still to be booked.
    """
    # rationale: the interpolated column list is a fixed string
    rows = conn.execute(  # nosec B608
        f"SELECT {_DELETION_COLUMNS}, sa.delete_status = 'deleting' AS claimed "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.removed_files IS NOT NULL "
        "  AND (sa.action != 'scheduled_deletion' OR sa.token_used = 0)"
    ).fetchall()
    found = [(_row_to_deletion_row(row), bool(row["claimed"])) for row in rows]
    # A record naming no file (malformed) has nothing to book.
    return [(row, claimed) for row, claimed in found if row.removed_paths]


def fetch_unbooked_media_ids(conn: sqlite3.Connection) -> set[str]:
    """Return the media items whose deletion removed files, or may have, and is not booked yet.

    Includes those deleted through Radarr/Sonarr whose delete intent is open:
    the reconcile books them from their rows.
    """
    begun = conn.execute(
        "SELECT media_item_id FROM scheduled_actions WHERE delete_status = 'deleting' "
        "UNION SELECT media_item_id FROM delete_intents WHERE completed_at IS NULL"
    )
    return {row["media_item_id"] for row in begun} | {
        row.media_item_id for row, _ in _fetch_unbooked(conn)
    }


def forget_unremoved_files(conn: sqlite3.Connection, media_ids: Collection[str]) -> None:
    """Leave the deletions of *media_ids* only the files they already removed.

    For items Plex no longer lists: no scan can say where their other files
    are, so the next pass leaves those alone and books what it removed. A
    claimed deletion keeps its files until recovery has read them.
    """
    for row, claimed in _fetch_unbooked(conn):
        if claimed or row.action != DELETION_ACTION or row.media_item_id not in media_ids:
            continue
        files = [
            {"path": path, "size": row.removed_sizes.get(path)}
            for path in sorted(row.removed_paths)
        ]
        conn.execute(
            "UPDATE media_items SET tracked_files = ? WHERE id = ?",
            (json.dumps(files), row.media_item_id),
        )


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
    False too once the item's library is unticked in Settings.
    """
    # rationale: the interpolated condition is a fixed string; every value is bound
    row = conn.execute(  # nosec B608
        "SELECT 1 FROM scheduled_actions sa JOIN media_items mi ON mi.id = sa.media_item_id "
        "WHERE sa.id = ? AND sa.action = 'scheduled_deletion' AND sa.token_used = 0 "
        f"  AND sa.delete_status = 'deleting' AND {LIBRARY_SELECTED}",
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
    delete state, since such rows are part of the same pass; not while its
    library is unticked, as the pass then holds it.
    """
    where, params = _at_or_under_candidates(path)
    # rationale: the interpolated condition is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        "SELECT mi.id, mi.title, mi.season_number, mi.file_path FROM media_items mi "
        f"WHERE mi.id != ? AND {where} "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM scheduled_actions sa "
        "    WHERE sa.media_item_id = mi.id AND sa.action = 'scheduled_deletion' "
        f"      AND sa.token_used = 0 AND sa.execute_at <= ? AND {LIBRARY_SELECTED}"
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
        f"SELECT {_DELETION_COLUMNS} "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        f"WHERE mi.id != ? AND sa.action = 'scheduled_deletion' AND {where} "
        f"AND {LIBRARY_SELECTED} ORDER BY sa.id",
        (media_item_id, *params),
    ).fetchall()
    candidates = [_row_to_deletion_row(row) for row in rows]
    return [
        row
        for row in candidates
        if _at_or_under(row.file_path, path) and claim_due_deletion(conn, row.id, now_iso)
    ]


# The path of a json_each entry, NULL for an entry that is not an object; the
# CASE keeps json_extract from ever reading a bare string as JSON text.
_ENTRY_PATH = "json_extract(CASE WHEN j.type = 'object' THEN j.value END, '$.path')"


def fetch_tracked_paths_in_folders(
    conn: sqlite3.Connection, folders: Collection[str], exclude_media_ids: Collection[str]
) -> set[str]:
    """Return the tracked file paths of other media items that sit directly in *folders*.

    Items in *exclude_media_ids* are left out. A row whose ``tracked_files``
    is NULL or malformed contributes nothing.
    """
    wanted = {folder.rstrip("/") or "/" for folder in folders if folder}
    if not wanted:
        return set()
    ranges = " OR ".join(f"({_ENTRY_PATH} >= ? AND {_ENTRY_PATH} < ?)" for _ in wanted)
    bounds = [bound for folder in sorted(wanted) for bound in (folder + "/", folder + "0")]
    excluded = list(exclude_media_ids)
    not_in = f"AND mi.id NOT IN ({','.join('?' * len(excluded))}) " if excluded else ""
    # rationale: only fixed expressions and "?" placeholders are interpolated; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT {_ENTRY_PATH} AS path FROM media_items mi, json_each("
        "  CASE WHEN json_valid(mi.tracked_files) THEN mi.tracked_files ELSE '[]' END) j "
        f"WHERE mi.tracked_files IS NOT NULL {not_in}AND ({ranges})",
        (*excluded, *bounds),
    ).fetchall()
    return {
        row["path"]
        for row in rows
        if isinstance(row["path"], str) and os.path.dirname(row["path"]) in wanted
    }


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


def _audit_removal(
    conn: sqlite3.Connection, row: DeletionRow, label: str, space_bytes: int | None
) -> int:
    rk = row.plex_rating_key
    detail = f"{label}: {row.title}" + (f" [rk:{rk}]" if rk else "")
    audit_id = log_audit(conn, row.media_item_id, "deleted", detail, space_bytes=space_bytes)
    record_deleted_media(
        conn,
        audit_id,
        media_type=row.media_type or "",
        tmdb_id=row.tmdb_id,
        tvdb_id=row.tvdb_id,
        season_number=row.season_number,
        title=row.title or "",
    )
    return audit_id


def book_deletion(
    conn: sqlite3.Connection,
    row: DeletionRow,
    label: str,
    space_bytes: int | None,
    *,
    unmonitor: bool = False,
) -> None:
    """Write the ``deleted`` audit entry with its identity row and drop the action row.

    With *unmonitor*, also leave a mark that Radarr/Sonarr is still to be told,
    which outlives the process. Does not commit: the caller's commit covers
    every write.
    """
    audit_id = _audit_removal(conn, row, label, space_bytes)
    if unmonitor:
        conn.execute(
            "INSERT INTO pending_unmonitors "
            "(audit_id, radarr_id, sonarr_id, tracked_files, removed_files) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                audit_id,
                row.radarr_id,
                row.sonarr_id,
                None if row.tracked_files is None else json.dumps(row.tracked_files),
                _removed_json(row.removed_paths, row.removed_bytes, row.removed_sizes, ()),
            ),
        )
    delete_scheduled_action(conn, row.id)


# The Radarr/Sonarr entry a mark waits to have rescanned; NULL while it is still to be told.
_RESCAN_ID = "CASE WHEN json_valid(removed_files) THEN json_extract(removed_files, '$.rescan') END"


def fetch_pending_unmonitors(conn: sqlite3.Connection) -> list[DeletionRow]:
    """Return each booked deletion Radarr/Sonarr is still to be told of; its ``id`` is its audit's."""
    # rationale: the interpolated condition is a fixed string
    rows = conn.execute(  # nosec B608
        "SELECT p.audit_id AS id, a.media_item_id, a.action, NULL AS file_path, "
        "NULL AS file_size_bytes, d.title, NULL AS plex_rating_key, p.radarr_id, p.sonarr_id, "
        "d.season_number, d.media_type, d.tmdb_id, d.tvdb_id, p.tracked_files, p.removed_files, "
        "1 AS library_selected "
        "FROM pending_unmonitors p JOIN audit_log a ON a.id = p.audit_id "
        "JOIN deleted_media d ON d.audit_id = p.audit_id "
        f"WHERE {_RESCAN_ID} IS NULL ORDER BY p.audit_id"
    ).fetchall()
    return [_row_to_deletion_row(row) for row in rows]


def has_pending_unmonitors(conn: sqlite3.Connection) -> bool:
    """Return True while a booked deletion waits to be unmonitored or rescanned."""
    return conn.execute("SELECT 1 FROM pending_unmonitors LIMIT 1").fetchone() is not None


def await_rescan(conn: sqlite3.Connection, audit_id: int, entry_id: int) -> bool:
    """Leave the mark waiting for a rescan of *entry_id* only; return False if it already is, or is gone."""
    # rationale: the interpolated condition is a fixed string; every value is bound
    cursor = conn.execute(  # nosec B608
        "UPDATE pending_unmonitors SET removed_files = ? "
        f"WHERE audit_id = ? AND {_RESCAN_ID} IS NULL",
        (json.dumps({"rescan": entry_id}), audit_id),
    )
    return cursor.rowcount == 1


def fetch_pending_rescans(conn: sqlite3.Connection, *, movies: bool) -> dict[int, list[int]]:
    """Return each Radarr (*movies*) or Sonarr entry to rescan, with the marks that wait for it."""
    # rationale: the interpolated expression is a fixed string; every value is bound
    rows = conn.execute(  # nosec B608
        f"SELECT p.audit_id, {_RESCAN_ID} AS entry_id FROM pending_unmonitors p "
        "JOIN deleted_media d ON d.audit_id = p.audit_id "
        "WHERE entry_id IS NOT NULL AND (d.media_type = 'movie') = ? ORDER BY entry_id",
        (movies,),
    ).fetchall()
    entries: dict[int, list[int]] = {}
    for row in rows:
        entries.setdefault(row["entry_id"], []).append(row["audit_id"])
    return entries


def finish_unmonitor(conn: sqlite3.Connection, audit_id: int) -> bool:
    """Drop the mark of the deletion booked as *audit_id*; return False if it is already gone."""
    cursor = conn.execute("DELETE FROM pending_unmonitors WHERE audit_id = ?", (audit_id,))
    return cursor.rowcount == 1


def was_audited_since(
    conn: sqlite3.Connection, audit_id: int, media_item_id: str, action: str, detail: str
) -> bool:
    """Return True if *media_item_id* has an entry of *action* with *detail* later than *audit_id*."""
    row = conn.execute(
        "SELECT 1 FROM audit_log "
        "WHERE media_item_id = ? AND id > ? AND action = ? AND detail = ? LIMIT 1",
        (media_item_id, audit_id, action, detail),
    ).fetchone()
    return row is not None


def was_requested_since(conn: sqlite3.Connection, row: DeletionRow) -> bool:
    """Return True if the title was downloaded again after *row*'s deletion was booked.

    *row* is as fetch_pending_unmonitors returns it. A download is audited
    under its TMDB/TVDB id, or under its bare title, which another movie or
    show may share: its audit detail, and the notification it left, say which.
    """
    tokens = redownload_tokens(
        DeletedMedia(
            row.id,
            row.media_type or "",
            row.tmdb_id,
            row.tvdb_id,
            row.season_number,
            row.title or "",
        )
    )
    keys = [row.title, *tokens]
    # rationale: only "?" placeholders are interpolated; every value is bound
    requests = conn.execute(  # nosec B608
        "SELECT media_item_id, detail FROM audit_log "
        "WHERE id > ? AND action IN ('downloaded', 're_downloaded') "
        f"AND media_item_id IN ({','.join('?' * len(keys))})",
        (row.id, *keys),
    ).fetchall()
    if any(request["media_item_id"] in tokens for request in requests):
        return True
    other = "Sonarr" if row.media_type == "movie" else "Radarr"
    if all(
        (request["detail"] or "").startswith(f"Added '{row.title}' to {other}")
        for request in requests
    ):
        return False
    notifications = conn.execute(
        "SELECT service, tmdb_id, tvdb_id, season_number FROM download_notifications "
        "WHERE title = ? AND created_at >= (SELECT created_at FROM audit_log WHERE id = ?)",
        (row.title, row.id),
    ).fetchall()
    return not notifications or any(_may_ask_for(found, row) for found in notifications)


def _may_ask_for(notification: sqlite3.Row, row: DeletionRow) -> bool:
    """Return False when the notification names another item than *row*'s: a service, id or season."""
    if row.media_type == "movie":
        service, column, own_id = "radarr", "tmdb_id", row.tmdb_id
        season = None
    else:
        service, column, own_id = "sonarr", "tvdb_id", row.tvdb_id
        season = notification["season_number"]
    asked_id = notification[column]
    return (
        notification["service"] == service
        and (asked_id is None or own_id is None or asked_id == own_id)
        and (season is None or row.season_number is None or season == row.season_number)
    )


def book_removed_files(
    conn: sqlite3.Connection, action_ids: Collection[object] | None = None
) -> None:
    """Book what the rows *action_ids* record as removed, and leave the rows without a record.

    Run it before a keep is written over a row, or a row is dropped, as its
    record would go with it. With no ids, books every row a keep was written
    over. A claimed row is left to the pass removing it, or to recovery. Does
    not commit.
    """
    for row, claimed in _fetch_unbooked(conn):
        wanted = row.action != DELETION_ACTION if action_ids is None else row.id in action_ids
        if claimed or not wanted:
            continue
        taken = conn.execute(
            "UPDATE scheduled_actions SET removed_files = NULL "
            "WHERE id = ? AND removed_files IS NOT NULL AND delete_status IS NOT 'deleting'",
            (row.id,),
        )
        if taken.rowcount:
            _audit_removal(conn, row, "Deleted", row.removed_bytes or None)


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
    not running now. Snoozes and protect-forever rows are not counted.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions "
            "WHERE action='scheduled_deletion' AND token_used=0 "
            "  AND delete_status IS NOT 'deleting'"
        ).fetchone()[0]
    )


class ClearedDeletions(NamedTuple):
    """Rows clear_pending_deletions found: those cleared while waiting, and those begun."""

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

    A row whose files are all removed has nothing left to cancel: it stays
    for the next pass to book and unmonitor, counted with those being
    removed. One with part of its files removed is booked with what it freed
    as it goes.

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
        unbooked = [found for found in _fetch_unbooked(conn) if found[0].action == DELETION_ACTION]
        finished = [(row, claimed) for row, claimed in unbooked if is_fully_removed(row)]
        waiting = sum(1 for _, claimed in finished if not claimed)
        cleared = count_pending_deletions(conn) - waiting
        already_deleting = waiting + int(
            conn.execute(
                "SELECT COUNT(*) FROM scheduled_actions "
                "WHERE action='scheduled_deletion' AND token_used=0 "
                "  AND delete_status = 'deleting'"
            ).fetchone()[0]
        )
        for row, _ in unbooked:
            if not is_fully_removed(row):
                book_deletion(conn, row, "Deleted", row.removed_bytes or None)
        # rationale: only "?" placeholders are interpolated; every value is bound
        conn.execute(  # nosec B608
            "DELETE FROM scheduled_actions WHERE action='scheduled_deletion' AND token_used=0 "
            f"AND id NOT IN ({','.join('?' * len(finished))})",
            [row.id for row, _ in finished],
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
