"""Deletion executor — removes files and reconciles the DB.

All deletion-time concerns live here:

* Stuck-state recovery (``_recover_stuck_deletions``) for rows left in
  the ``deleting`` state by a previous crash between the on-disk rm and
  the DB cleanup commit.
* Allowlist enforcement via :func:`repository.read_delete_allowed_roots_setting`.
* The two-phase delete loop that unmonitors *arr clients only after the
  SQLite write lock has been released.

Imports :mod:`repository`; MUST NOT import :mod:`fetch` or
:mod:`engine` (see engine.py header).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import TYPE_CHECKING, TypedDict

import requests

from mediaman.core.audit import log_audit
from mediaman.core.time import now_utc
from mediaman.scanner import repository
from mediaman.services.arr.base import ArrError
from mediaman.services.infra import DeletionRefused, SafeHTTPError, delete_path

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient

logger = logging.getLogger(__name__)


class DeletionResult(TypedDict):
    """Return type of :meth:`DeletionExecutor.execute`."""

    deleted: int
    reclaimed_bytes: int


def _recover_stuck_deletions(conn: sqlite3.Connection) -> None:
    """Reconcile ``scheduled_actions`` rows left in the ``deleting`` state.

    Called at the start of :meth:`DeletionExecutor.execute` and by the
    scheduler on startup. For each row marked ``deleting`` we check
    whether the on-disk file is still present:

    * File absent -> the rm completed but the follow-up bookkeeping was
      never committed. Convert to a normal ``deleted`` cleanup: write
      the audit entry and drop the row.
    * File present -> the rm never ran. Reset to ``pending`` so the next
      normal run retries cleanly.

    Idempotent; safe to call on every startup. Does not itself delete
    any files — purely a state reconciliation.
    """
    rows = repository.fetch_stuck_deletions(conn)
    if not rows:
        return

    for row in rows:
        file_path = row["file_path"] or ""
        file_present = bool(file_path) and os.path.lexists(file_path)
        if file_present:
            logger.warning(
                "engine.delete.recover id=%s path=%r — file still present, "
                "reverting status to 'pending'",
                row["id"],
                file_path,
            )
            repository.mark_delete_status(conn, row["id"], "pending")
        else:
            logger.warning(
                "engine.delete.recover id=%s path=%r — file already gone, completing cleanup",
                row["id"],
                file_path,
            )
            rk = row["plex_rating_key"]
            detail = f"Deleted (recovered): {row['title']}" + (f" [rk:{rk}]" if rk else "")
            log_audit(
                conn,
                row["media_item_id"],
                "deleted",
                detail,
                space_bytes=row["file_size_bytes"],
            )
            repository.delete_scheduled_action(conn, row["id"])
    conn.commit()


def _delete_file_on_disk(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    allowed_roots: list[str],
) -> bool:
    """Attempt to delete the on-disk file; return True on success.

    On any error the row is left in the appropriate state and False is
    returned so the caller can ``continue`` to the next row.  All
    try/except branches are contained here so no except clause is ever
    stranded in a different scope from its try.
    # rationale: four distinct exception types (DeletionRefused, FileNotFoundError,
    # PermissionError/OSError, Exception) each require different recovery
    # actions; splitting by exception type would separate try from except.
    """
    try:
        delete_path(row["file_path"], allowed_roots=allowed_roots)
        return True
    except DeletionRefused as exc:
        # Allowlist refusal: the path is wrong, but the action
        # row is still valid — reset to pending so a later run
        # (e.g. once the operator fixes the path) can retry.
        logger.error(
            "Refusing to delete '%s' — path is outside configured delete_allowed_roots: %s",
            row["file_path"],
            exc,
        )
        repository.mark_delete_status(conn, row["id"], "pending")
        conn.commit()
        return False
    except FileNotFoundError as exc:
        # The file vanished between fetch and rm — likely
        # deleted out-of-band. The standard recovery path
        # (_recover_stuck_deletions sees file absent → marks
        # deleted) handles this cleanly on the next run, so
        # leave the row in 'deleting' state. Treated as
        # transient because the action row itself is still
        # valid and a future run will reconcile it.
        logger.info(
            "engine.delete.file_missing id=%s path=%r — "
            "leaving row in 'deleting' state; next run will "
            "complete cleanup via _recover_stuck_deletions: %s",
            row["id"],
            row["file_path"],
            exc,
        )
        return False
    except (PermissionError, OSError) as exc:
        # Likely permanent without operator intervention
        # (read-only filesystem, ACL refusal, low-level I/O
        # error, IsADirectoryError, etc.). Leave the row in
        # 'deleting' state so a subsequent run can inspect via
        # _recover_stuck_deletions (file present → reset to
        # pending; file gone → mark deleted), and emit an
        # explicit audit entry now so the failure is visible
        # to the operator without waiting for the next scan.
        logger.exception(
            "engine.delete.permanent_failure id=%s path=%r — "
            "leaving row in 'deleting' state for next run "
            "to inspect (file may need manual intervention)",
            row["id"],
            row["file_path"],
        )
        log_audit(
            conn,
            row["media_item_id"],
            "delete_failed_permanent",
            f"Permanent delete error: {row['title']} — {exc.__class__.__name__}: {exc}",
        )
        conn.commit()
        return False
    except Exception as exc:  # rationale: documented permanent-failure path — operator must see every unhandled deletion failure
        # Unexpected exception type. Log + audit and treat as
        # permanent so the operator can investigate; leave the
        # row in 'deleting' for recovery to reconcile.
        logger.exception(
            "engine.delete.unexpected_failure id=%s path=%r — "
            "leaving row in 'deleting' state for next run to "
            "inspect (programming error or unhandled exception)",
            row["id"],
            row["file_path"],
        )
        log_audit(
            conn,
            row["media_item_id"],
            "delete_failed_permanent",
            f"Unexpected delete error: {row['title']} — {exc.__class__.__name__}: {exc}",
        )
        conn.commit()
        return False


def _commit_deletion(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Write the audit entry, drop the scheduled_action row, and commit.

    Called after a successful on-disk delete.  The commit closes the
    write lock *before* the best-effort *arr unmonitor HTTP calls.
    """
    # Record the deletion and close the transaction *before* the
    # Radarr/Sonarr unmonitor HTTP calls. The unmonitor is
    # best-effort housekeeping — a failure (or slow response)
    # must not keep the SQLite write lock open.
    rk = row["plex_rating_key"]
    detail = f"Deleted: {row['title']}" + (f" [rk:{rk}]" if rk else "")
    log_audit(
        conn,
        row["media_item_id"],
        "deleted",
        detail,
        space_bytes=row["file_size_bytes"],
    )
    repository.delete_scheduled_action(conn, row["id"])
    conn.commit()


def _unmonitor_arr(
    row: sqlite3.Row,
    radarr_client: ArrClient | None,
    sonarr_client: ArrClient | None,
) -> None:
    """Send best-effort unmonitor calls to Radarr/Sonarr.

    Failures are non-fatal: logged as warnings and swallowed so a
    slow or unavailable *arr instance never blocks the deletion loop.
    Must be called *after* the SQLite write lock has been released
    (i.e. after ``_commit_deletion``).
    """
    # Unmonitor in *arr clients — failures are non-fatal and
    # happen outside any open transaction.
    if row["radarr_id"] and radarr_client:
        try:
            radarr_client.unmonitor_movie(row["radarr_id"])
        except (SafeHTTPError, requests.RequestException, ArrError):
            logger.warning(
                "Failed to unmonitor movie %s after deletion",
                row["radarr_id"],
                exc_info=True,
            )

    if row["sonarr_id"] and row["season_number"] is not None and sonarr_client:
        try:
            sonarr_client.unmonitor_season(row["sonarr_id"], row["season_number"])
        except (SafeHTTPError, requests.RequestException, ArrError):
            logger.warning(
                "Failed to unmonitor season %s of series %s after deletion",
                row["season_number"],
                row["sonarr_id"],
                exc_info=True,
            )


class DeletionExecutor:
    """Executes pending deletions whose grace period has elapsed.

    Encapsulates the previously in-engine ``execute_deletions`` loop:
    allowlist read, stuck-state recovery, two-phase on-disk rm, audit
    logging, and best-effort Radarr/Sonarr unmonitor calls.

    Args:
        conn: Open SQLite connection.
        dry_run: When True, skip the on-disk rm (and the row deletion +
            audit-log entry that would normally follow). A
            ``dry_run_skip`` audit row is written instead so an operator
            can see what *would* have been deleted. Snooze cleanup is
            controlled separately via ``cleanup_snoozes``.
        cleanup_snoozes: When True (default), expired snoozes are
            removed so the items re-enter the scan pipeline. Set to
            False when the caller is performing a true dry-run preview
            and must not mutate ``scheduled_actions``.
        sonarr_client: Optional Sonarr API client for unmonitor calls.
        radarr_client: Optional Radarr API client for unmonitor calls.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        dry_run: bool = False,
        cleanup_snoozes: bool = True,
        sonarr_client: ArrClient | None = None,
        radarr_client: ArrClient | None = None,
    ) -> None:
        self._conn = conn
        self._dry_run = dry_run
        self._cleanup_snoozes = cleanup_snoozes
        self._sonarr = sonarr_client
        self._radarr = radarr_client

    def execute(self) -> DeletionResult:
        """Run the deletion pass.

        Returns a dict with ``deleted`` count and ``reclaimed_bytes``
        total. Cleans up expired snoozes before returning unless
        ``cleanup_snoozes`` was set to ``False`` at construction, ensuring
        a real dry-run preview never mutates ``scheduled_actions``.

        # rationale: orchestrator — body is sequential phase calls plus
        # the deletion loop; the loop counter state (deleted_count,
        # reclaimed_bytes) spans all phases and ties them together.

        Pipeline:
        1. Load allowlist + recover stuck rows.
        2. For each pending row in dry-run mode, write audit and skip.
        3. For each live row: mark 'deleting', delete file on disk.
        4. On success: commit deletion audit + drop action row.
        5. Best-effort *arr unmonitor (outside any transaction).
        """
        now = now_utc()
        deleted_count = 0
        reclaimed_bytes = 0

        allowed_roots = repository.read_delete_allowed_roots_setting(self._conn)

        # Recover any rows left in the 'deleting' state by a previous
        # crash between the on-disk rm and the DB cleanup commit.
        _recover_stuck_deletions(self._conn)

        rows = repository.fetch_pending_deletions(self._conn, now.isoformat())

        for row in rows:
            if self._dry_run:
                log_audit(
                    self._conn,
                    row["media_item_id"],
                    "dry_run_skip",
                    f"Would delete: {row['title']}",
                )
                continue

            # Remove files from disk.
            if not allowed_roots:
                logger.error(
                    "Skipping deletion of '%s': delete_allowed_roots not "
                    "configured. Set the setting or `MEDIAMAN_DELETE_ROOTS` "
                    "env var.",
                    row["file_path"],
                )
                continue

            # Two-phase delete: mark the row 'deleting' and commit BEFORE
            # removing the file. If we crash between this commit and the
            # rm, the next run's _recover_stuck_deletions() can inspect
            # the row and decide whether the file is still there (reset
            # to pending) or already gone (mark deleted).
            logger.info(
                "engine.delete.intent id=%s media_id=%s path=%r",
                row["id"],
                row["media_item_id"],
                row["file_path"],
            )
            repository.mark_delete_status(self._conn, row["id"], "deleting")
            self._conn.commit()

            if not _delete_file_on_disk(self._conn, row, allowed_roots):
                continue

            _commit_deletion(self._conn, row)
            _unmonitor_arr(row, self._radarr, self._sonarr)

            deleted_count += 1
            reclaimed_bytes += row["file_size_bytes"] or 0

        # Remove expired snoozes so items re-enter the scan pipeline.
        # A real dry-run preview must NOT mutate scheduled_actions, so
        # the engine passes ``cleanup_snoozes=False`` when running in
        # dry_run mode.
        if self._cleanup_snoozes:
            repository.cleanup_expired_snoozes(self._conn, now.isoformat())

        self._conn.commit()
        return {"deleted": deleted_count, "reclaimed_bytes": reclaimed_bytes}
