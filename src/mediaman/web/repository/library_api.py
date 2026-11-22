"""Repository functions for the library JSON API routes.

Covers SQL operations driving the keep, delete, and redownload flows in
`mediaman.web.routes.library_api`. Tables touched here:
``media_items``, ``scheduled_actions``, ``audit_log``, and
``download_notifications`` (via `record_download_notification`).

Route handlers orchestrate and do not query. Multi-statement work runs inside a
single ``with conn:`` block so the audit row and the business mutation land in
the same commit.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from mediaman.core.audit import log_audit
from mediaman.core.deleted_media import record_deleted_media
from mediaman.scanner.repository import is_protected, is_show_kept_pure
from mediaman.services.downloads.notifications import record_download_notification


class NotFound(Exception):
    """Raised when a media row that the route expected to read is absent.

    The keep and delete handlers map this onto their respective wire
    responses (404 for keep, 403 for delete to avoid leaking existence).
    Defined here rather than at the call site so the SQL function can
    raise inside ``with conn:`` and the route does not need a generic
    ``RuntimeError`` sentinel.
    """


class Kept(Exception):
    """Raised when a delete targets an item the scanner would keep."""


class DeletionRunning(Exception):
    """Raised when the scheduled deletion pass is removing the item right now."""


@dataclass(frozen=True)
class MediaDeleteSnapshot:
    """Captured media_items row used to drive the delete flow.

    The snapshot is taken under a write-locked transaction so the external
    Radarr/Sonarr call can run without holding a SQLite lock. Every
    identifier the cleanup transaction needs is denormalised here so the
    delete path never re-reads the row after the lock has been released.
    """

    title: str
    media_type: str
    file_path: str | None
    file_size_bytes: int | None
    radarr_id: int | None
    sonarr_id: int | None
    season_number: int | None
    plex_rating_key: str | None
    show_rating_key: str | None
    tmdb_id: int | None
    tvdb_id: int | None
    tracked_files: list[dict[str, object]] | None


def apply_keep_in_tx(
    conn: sqlite3.Connection,
    *,
    media_id: str,
    action: str,
    execute_at: str | None,
    now_iso: str,
    snooze_label: str,
    new_token: str,
    audit_detail: str,
    actor: str,
) -> bool:
    """Apply a keep / snooze decision atomically with an audit row.

    Looks for an existing ``scheduled_actions`` row for *media_id* with
    ``token_used = 0`` and updates it in place if present; otherwise
    inserts a fresh row.  The audit row is written inside the same
    transaction so a SQLite failure rolls the keep back along with the
    audit record.

    Returns True when the deletion pass is removing the item right now. The
    keep is still written: if that removal fails, the item stays kept.

    Raises:
        NotFound: when the target media_items row does not exist. The
            ``with conn:`` block rolls back any partial writes.
    """
    # ``with conn:`` commits on normal exit and rolls back on exception;
    # BEGIN IMMEDIATE here preserves write-lock semantics.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        media_row = conn.execute("SELECT id FROM media_items WHERE id = ?", (media_id,)).fetchone()
        if media_row is None:
            raise NotFound(media_id)

        existing = conn.execute(
            "SELECT id, delete_status FROM scheduled_actions "
            "WHERE media_item_id = ? AND token_used = 0",
            (media_id,),
        ).fetchall()
        deletion_running = any(row["delete_status"] == "deleting" for row in existing)

        if existing:
            conn.execute(
                "UPDATE scheduled_actions "
                "SET action=?, execute_at=?, snoozed_at=?, snooze_duration=?, token_used=0 "
                "WHERE id=?",
                (action, execute_at, now_iso, snooze_label, existing[0]["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO scheduled_actions "
                "(media_item_id, action, scheduled_at, execute_at, token, token_used, "
                "snoozed_at, snooze_duration) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    media_id,
                    action,
                    now_iso,
                    execute_at,
                    new_token,
                    now_iso,
                    snooze_label,
                ),
            )

        log_audit(
            conn,
            media_id,
            "snoozed",
            audit_detail,
            actor=actor,
        )
    return deletion_running


def _parse_tracked_files(raw: str | None) -> list[dict[str, object]] | None:
    """Decode ``media_items.tracked_files``; ``None`` when absent or unreadable."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, list):
        return None
    return [entry for entry in value if isinstance(entry, dict)]


def _refuse_if_not_deletable(
    conn: sqlite3.Connection, media_id: str, show_rating_key: str | None
) -> None:
    """Raise when the item is kept or the deletion pass is removing it now.

    A delete racing the deletion pass would book the item deleted twice.
    """
    if is_protected(conn, media_id) or is_show_kept_pure(conn, show_rating_key):
        raise Kept(media_id)
    running = conn.execute(
        "SELECT 1 FROM scheduled_actions WHERE media_item_id = ? AND delete_status = 'deleting'",
        (media_id,),
    ).fetchone()
    if running is not None:
        raise DeletionRunning(media_id)


def snapshot_media_for_delete(conn: sqlite3.Connection, media_id: str) -> MediaDeleteSnapshot:
    """Read the media row needed for a delete and release the write-lock.

    Runs under ``BEGIN IMMEDIATE`` so concurrent writers cannot mutate the
    row between read and release; ``with conn:`` commits on normal exit
    (or rolls back on `NotFound`).

    An item the scanner keeps (its own keep, or a keep on its show) is
    refused: dropping its row and keep would let the next scan re-add it
    unkept and schedule its files for deletion.

    Raises:
        NotFound: when no row matches *media_id*.
        Kept: when the item is kept.
        DeletionRunning: when the deletion pass is removing the item.
    """
    # Snapshot transaction: ``with conn:`` commits on normal exit and
    # rolls back on exception; BEGIN IMMEDIATE preserves write-lock
    # semantics.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, title, media_type, file_path, file_size_bytes, "
            "radarr_id, sonarr_id, season_number, plex_rating_key, show_rating_key, "
            "tmdb_id, tvdb_id, tracked_files "
            "FROM media_items WHERE id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            raise NotFound(media_id)
        _refuse_if_not_deletable(conn, media_id, row["show_rating_key"])
        return MediaDeleteSnapshot(
            title=row["title"],
            media_type=row["media_type"],
            file_path=row["file_path"],
            file_size_bytes=row["file_size_bytes"],
            radarr_id=row["radarr_id"],
            sonarr_id=row["sonarr_id"],
            season_number=row["season_number"],
            plex_rating_key=row["plex_rating_key"],
            show_rating_key=row["show_rating_key"],
            tmdb_id=row["tmdb_id"],
            tvdb_id=row["tvdb_id"],
            tracked_files=_parse_tracked_files(row["tracked_files"]),
        )


def recheck_deletable(conn: sqlite3.Connection, media_id: str) -> None:
    """Check again, just before the Radarr/Sonarr delete, that nothing now forbids it.

    A keep or a deletion run may have started while the item was being
    checked against Radarr/Sonarr.

    Raises:
        NotFound: when the row is gone.
        Kept: when the item is now kept.
        DeletionRunning: when the deletion pass is now removing the item.
    """
    row = conn.execute(
        "SELECT show_rating_key FROM media_items WHERE id = ?", (media_id,)
    ).fetchone()
    if row is None:
        raise NotFound(media_id)
    _refuse_if_not_deletable(conn, media_id, row["show_rating_key"])


def show_has_other_items(
    conn: sqlite3.Connection,
    media_id: str,
    *,
    show_rating_key: str | None,
    tvdb_id: int | None,
) -> bool:
    """True when mediaman holds another row of the same show as *media_id*."""
    row = conn.execute(
        "SELECT 1 FROM media_items WHERE id != ? AND (show_rating_key = ? OR tvdb_id = ?)",
        (media_id, show_rating_key, tvdb_id),
    ).fetchone()
    return row is not None


def tracked_files_of_same_title(
    conn: sqlite3.Connection,
    media_id: str,
    *,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
    season_number: int | None = None,
) -> list[list[dict[str, object]]]:
    """Return the tracked files of each other row with the same TMDB id (a movie) or
    the same TVDB id and season (a season) as *media_id*.
    """
    if tmdb_id is not None:
        rows = conn.execute(
            "SELECT tracked_files FROM media_items "
            "WHERE id != ? AND media_type = 'movie' AND tmdb_id = ?",
            (media_id, tmdb_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT tracked_files FROM media_items "
            "WHERE id != ? AND tvdb_id = ? AND season_number = ?",
            (media_id, tvdb_id, season_number),
        ).fetchall()
    return [_parse_tracked_files(row["tracked_files"]) or [] for row in rows]


def other_item_uses_paths(
    conn: sqlite3.Connection,
    media_id: str,
    *,
    folders: Iterable[str] = (),
    files: Iterable[str] = (),
) -> bool:
    """True when a row other than *media_id* has its ``file_path`` or a tracked file
    equal to one of *files*, or at or inside one of *folders*.
    """
    folder_list = [f.rstrip("/") for f in folders if f.rstrip("/")]
    file_set = set(files)
    if not folder_list and not file_set:
        return False
    rows = conn.execute(
        "SELECT file_path, tracked_files FROM media_items WHERE id != ?", (media_id,)
    )
    for row in rows:
        tracked = _parse_tracked_files(row["tracked_files"]) or []
        for path in [row["file_path"], *(entry.get("path") for entry in tracked)]:
            if not isinstance(path, str) or not path:
                continue
            if path in file_set or any(
                path == folder or path.startswith(folder + "/") for folder in folder_list
            ):
                return True
    return False


def finalise_delete_in_tx(
    conn: sqlite3.Connection,
    snapshot: MediaDeleteSnapshot,
    *,
    media_id: str,
    audit_detail: str,
    actor: str,
) -> None:
    """Run the cleanup transaction: audit, deleted-media identity, prune scheduled_actions,
    drop row.

    Called after the external Radarr/Sonarr delete succeeds (or is detected
    as already-gone), so the files are gone and the row must go too. All
    writes land in the same commit so a SQLite failure rolls the lot back.
    """
    # Cleanup transaction: ``with conn:`` commits on normal exit and
    # rolls back on exception; BEGIN IMMEDIATE preserves write-lock
    # semantics.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        audit_id = log_audit(
            conn,
            media_id,
            "deleted",
            audit_detail,
            space_bytes=snapshot.file_size_bytes,
            actor=actor,
        )
        record_deleted_media(
            conn,
            audit_id,
            media_type=snapshot.media_type,
            tmdb_id=snapshot.tmdb_id,
            tvdb_id=snapshot.tvdb_id,
            season_number=snapshot.season_number,
            title=snapshot.title,
        )
        conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_id,))
        conn.execute("DELETE FROM media_items WHERE id = ?", (media_id,))


def record_redownload(
    conn: sqlite3.Connection,
    *,
    audit_id: str,
    audit_detail: str,
    actor: str,
    email: str,
    title: str,
    media_type: str,
    service: str,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
) -> None:
    """Persist the audit row and download-notification claim for a redownload.

    Audit and notification rows are written under a single commit so the
    user-facing "added to Radarr/Sonarr" response is never returned with
    only half of the bookkeeping landed.
    """
    with conn:
        log_audit(
            conn,
            audit_id,
            "re_downloaded",
            audit_detail,
            actor=actor,
        )
        record_download_notification(
            conn,
            email=email,
            title=title,
            media_type=media_type,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            service=service,
        )
