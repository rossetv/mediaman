"""Deletion executor — removes files and reconciles the DB.

All deletion-time concerns live here:

* Stuck-state recovery (recover_stuck_deletions) for rows left in
  the ``deleting`` state by a previous crash between the on-disk rm and
  the DB cleanup commit.
* Allowlist enforcement via repository.read_delete_allowed_roots_setting.
* The two-phase delete loop that unmonitors *arr clients only after the
  SQLite write lock has been released.

Imports repository; MUST NOT import fetch or
engine (see engine.py header).
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
from mediaman.scanner.repository import DeletionRow
from mediaman.scanner.repository.scheduled_actions import (
    DELETION_ACTION,
    cancel_kept_deletion,
    claim_due_deletion,
    claim_due_deletions_under,
    fetch_media_held_outside_pass,
    is_claimed_deletion,
    is_kept_deletion,
    is_latest_audit,
)
from mediaman.services.arr.base import ArrError
from mediaman.services.infra import (
    DeletionRefused,
    SafeHTTPError,
    delete_path,
    path_within_delete_roots,
)
from mediaman.services.infra.storage import delete_root_available

if TYPE_CHECKING:
    from datetime import datetime

    from mediaman.services.arr.base import ArrClient

logger = logging.getLogger(__name__)


class DeletionResult(TypedDict):
    deleted: int
    reclaimed_bytes: int


def recover_stuck_deletions(conn: sqlite3.Connection) -> None:
    """Reconcile ``scheduled_actions`` rows left in the ``deleting`` state.

    Called at the start of a *live* DeletionExecutor.execute pass
    (never under dry-run — a preview mutates nothing) and by the
    scheduler on startup. For each row marked ``deleting`` we check
    whether the on-disk file is still present:

    * File absent **and** the stored path was within the configured
      delete roots -> the rm completed but the follow-up bookkeeping was
      never committed. Convert to a normal ``deleted`` cleanup: write the
      audit entry (crediting the freed bytes) and drop the row. If a keep or
      snooze was written over the row during the removal, the keep stands:
      audit that the media is gone and reset to ``pending``, never booking
      it as a scheduled deletion.
    * File absent **but** the stored path was NOT within the configured
      delete roots (empty path, or an operator changed
      ``MEDIAMAN_DELETE_ROOTS`` between the crash and restart, or the row's
      path was always bogus) -> mediaman could never have deleted it, so we
      must NOT fabricate a "deleted N bytes" audit row (that would corrupt
      the audit trail and the reclaimed-bytes stats — a security
      surface). Reset to ``pending`` and log loudly instead.
    * File absent **but** its delete root is missing or empty -> the media
      mount is likely down, so the absence proves nothing. Reset to
      ``pending``.
    * File present -> the rm never ran. Reset to ``pending`` so the next
      normal run retries cleanly.

    Idempotent; safe to call on every startup. Does not itself delete
    any files — purely a state reconciliation. Each row is committed before
    the next row's disk checks, so a hung mount never holds the write lock.
    """
    rows = repository.fetch_stuck_deletions(conn)
    if not rows:
        return

    # Read the allowlist once so the file-absent branch can confirm the
    # stored path was ever deletable before crediting freed bytes.
    allowed_roots = repository.read_delete_allowed_roots_setting(conn)

    for row in rows:
        file_path = row.file_path or ""
        file_present = bool(file_path) and os.path.lexists(file_path)
        if file_present:
            logger.warning(
                "engine.delete.recover id=%s path=%r — file still present, "
                "reverting status to 'pending'",
                row.id,
                file_path,
            )
            repository.mark_delete_status(conn, row.id, "pending")
        elif not path_within_delete_roots(file_path, allowed_roots):
            # File is gone but the path was never inside an allowed root —
            # mediaman did not (and could not) delete it. Do NOT write a
            # phantom "deleted" audit row. Reset to pending and shout.
            logger.error(
                "engine.delete.recover id=%s path=%r — file absent but path is "
                "outside the configured delete_allowed_roots (or roots are "
                "unconfigured). Refusing to fabricate a 'deleted' audit entry; "
                "reverting status to 'pending' for operator review.",
                row.id,
                file_path,
            )
            repository.mark_delete_status(conn, row.id, "pending")
        elif not delete_root_available(file_path, allowed_roots):
            logger.error(
                "engine.delete.recover id=%s path=%r — file absent but its delete "
                "root is missing or empty (media mount down?); reverting status "
                "to 'pending' instead of booking it as deleted.",
                row.id,
                file_path,
            )
            repository.mark_delete_status(conn, row.id, "pending")
        elif row.action != DELETION_ACTION:
            logger.warning(
                "engine.delete.recover id=%s path=%r action=%r — file gone, but the "
                "item was kept during its removal; keeping the row",
                row.id,
                file_path,
                row.action,
            )
            rk = row.plex_rating_key
            log_audit(
                conn,
                row.media_item_id,
                "media_missing",
                f"Files gone (recovered): {row.title} — kept while it was being deleted"
                + (f" [rk:{rk}]" if rk else ""),
            )
            repository.mark_delete_status(conn, row.id, "pending")
        else:
            logger.warning(
                "engine.delete.recover id=%s path=%r — file already gone, completing cleanup",
                row.id,
                file_path,
            )
            rk = row.plex_rating_key
            detail = f"Deleted (recovered): {row.title}" + (f" [rk:{rk}]" if rk else "")
            log_audit(
                conn,
                row.media_item_id,
                "deleted",
                detail,
                space_bytes=row.file_size_bytes,
            )
            repository.delete_scheduled_action(conn, row.id)
        conn.commit()


# rationale: four distinct exception types (DeletionRefused, FileNotFoundError,
# PermissionError/OSError, Exception) each require different recovery
# actions; splitting by exception type would separate try from except.
def _delete_file_on_disk(
    conn: sqlite3.Connection,
    row: DeletionRow,
    allowed_roots: list[str],
) -> bool:
    """Attempt to delete the on-disk file; return True on success.

    On any error the row is left in the appropriate state and False is
    returned so the caller can ``continue`` to the next row. A failed removal
    goes back to ``pending``: the pass reads its rows once, so the next pass
    retries it. ``deleting`` means a removal running now, or a leftover whose
    files are already gone (missing at the rm, or a crash) awaiting recovery.  All
    try/except branches are contained here so no except clause is ever
    stranded in a different scope from its try.
    """
    try:
        delete_path(row.file_path or "", allowed_roots=allowed_roots)
        return True
    except DeletionRefused as exc:
        # Allowlist refusal: the path is wrong, but the action
        # row is still valid — reset to pending so a later run
        # (e.g. once the operator fixes the path) can retry.
        logger.error(
            "Refusing to delete '%s' — path is outside configured delete_allowed_roots: %s",
            row.file_path,
            exc,
        )
        repository.mark_delete_status(conn, row.id, "pending")
        conn.commit()
        return False
    except FileNotFoundError as exc:
        # The file vanished between fetch and rm — likely
        # deleted out-of-band. The standard recovery path
        # (recover_stuck_deletions sees file absent → marks
        # deleted) handles this cleanly on the next run, so
        # leave the row in 'deleting' state. Treated as
        # transient because the action row itself is still
        # valid and a future run will reconcile it.
        logger.info(
            "engine.delete.file_missing id=%s path=%r — "
            "leaving row in 'deleting' state; next run will "
            "complete cleanup via recover_stuck_deletions: %s",
            row.id,
            row.file_path,
            exc,
        )
        return False
    except (PermissionError, OSError) as exc:
        # Likely permanent without operator intervention
        # (read-only filesystem, ACL refusal, low-level I/O
        # error, IsADirectoryError, etc.). Audit it now so the
        # failure is visible to the operator without waiting
        # for the next scan.
        logger.exception(
            "engine.delete.permanent_failure id=%s path=%r — "
            "resetting row to 'pending' for the next run to "
            "retry (file may need manual intervention)",
            row.id,
            row.file_path,
        )
        repository.mark_delete_status(conn, row.id, "pending")
        log_audit(
            conn,
            row.media_item_id,
            "delete_failed_permanent",
            f"Permanent delete error: {row.title} — {exc.__class__.__name__}: {exc}",
        )
        conn.commit()
        return False
    except Exception as exc:
        # Unexpected exception type. Log + audit and treat as
        # permanent so the operator can investigate.
        logger.exception(
            "engine.delete.unexpected_failure id=%s path=%r — "
            "resetting row to 'pending' for the next run to "
            "retry (programming error or unhandled exception)",
            row.id,
            row.file_path,
        )
        repository.mark_delete_status(conn, row.id, "pending")
        log_audit(
            conn,
            row.media_item_id,
            "delete_failed_permanent",
            f"Unexpected delete error: {row.title} — {exc.__class__.__name__}: {exc}",
        )
        conn.commit()
        return False


def _commit_deletion(conn: sqlite3.Connection, row: DeletionRow) -> None:
    """Write the audit entry, drop the scheduled_action row, and commit.

    Called after a successful on-disk delete.  The commit closes the
    write lock *before* the best-effort *arr unmonitor HTTP calls.
    """
    # Record the deletion and close the transaction *before* the
    # Radarr/Sonarr unmonitor HTTP calls. The unmonitor is
    # best-effort housekeeping — a failure (or slow response)
    # must not keep the SQLite write lock open.
    rk = row.plex_rating_key
    detail = f"Deleted: {row.title}" + (f" [rk:{rk}]" if rk else "")
    log_audit(
        conn,
        row.media_item_id,
        "deleted",
        detail,
        space_bytes=row.file_size_bytes,
    )
    repository.delete_scheduled_action(conn, row.id)
    conn.commit()


def _unmonitor_arr(
    row: DeletionRow,
    *,
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
    if row.radarr_id and radarr_client:
        try:
            radarr_client.unmonitor_movie(row.radarr_id)
        except (SafeHTTPError, requests.RequestException, ArrError):
            logger.warning(
                "Failed to unmonitor movie %s after deletion",
                row.radarr_id,
                exc_info=True,
            )

    if row.sonarr_id and row.season_number is not None and sonarr_client:
        try:
            sonarr_client.unmonitor_season(row.sonarr_id, row.season_number)
        except (SafeHTTPError, requests.RequestException, ArrError):
            logger.warning(
                "Failed to unmonitor season %s of series %s after deletion",
                row.season_number,
                row.sonarr_id,
                exc_info=True,
            )


def _log_root_unavailable(row: DeletionRow, path: str) -> None:
    logger.error(
        "engine.delete.root_unavailable id=%s path=%r — target is missing and "
        "its delete root is missing or empty (media mount down?); leaving "
        "the row pending",
        row.id,
        path,
    )


class DeletionExecutor:
    """Executes pending deletions whose grace period has elapsed.

    Runs the deletion loop:
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

        Pipeline:
        1. Recover stuck rows (skipped in dry-run — preview mutates nothing).
        2. Dry-run: write a ``dry_run_skip`` audit per pending row and stop.
        3. Live: load allowlist (fail closed if unset), then for each row
           mark 'deleting', delete file on disk, commit audit + unmonitor.
        4. Clean up expired snoozes unless dry-run.
        """
        now = now_utc()
        rows = self._recover_then_fetch_pending(now)
        if self._dry_run:
            # Preview: record what *would* be deleted, mutate no state.
            for row in rows:
                log_audit(
                    self._conn, row.media_item_id, "dry_run_skip", f"Would delete: {row.title}"
                )
            self._conn.commit()
            return {"deleted": 0, "reclaimed_bytes": 0}
        return self._execute_live(rows, now)

    def _recover_then_fetch_pending(self, now: datetime) -> list[DeletionRow]:
        """Recover stuck rows (live only) and return the pending-deletion set.

        Recovery commits real state changes, so a dry-run preview must NOT
        run it: the periodic 30-min library sync is a dry-run and
        genuine crash recovery already runs at boot via the scheduler and at
        the start of every live deletion pass below. Guarding here keeps the
        documented "dry-run mutates nothing" contract honest.
        """
        if not self._dry_run:
            recover_stuck_deletions(self._conn)
        return repository.fetch_pending_deletions(self._conn, now.isoformat())

    def _execute_live(self, rows: list[DeletionRow], now: datetime) -> DeletionResult:
        """Delete each eligible row from disk, fail-closed on an empty allowlist."""
        deleted_count = 0
        reclaimed_bytes = 0
        allowed_roots = repository.read_delete_allowed_roots_setting(self._conn)
        # Fail closed once, before the loop: deletion must refuse when
        # MEDIAMAN_DELETE_ROOTS is unset. Checked outside the per-row
        # loop so the refusal is logged a single time, not once per pending
        # row. Snooze cleanup below still runs so expired snoozes
        # re-enter the pipeline once roots are configured.
        if not allowed_roots:
            if rows:
                logger.error(
                    "Skipping %d pending deletion(s): delete_allowed_roots not "
                    "configured. Set the setting or `MEDIAMAN_DELETE_ROOTS` env var.",
                    len(rows),
                )
        else:
            for row in rows:
                for closed in self._delete_one(row, allowed_roots, now.isoformat()):
                    deleted_count += 1
                    reclaimed_bytes += closed.file_size_bytes or 0
        if self._cleanup_snoozes:
            repository.cleanup_expired_snoozes(self._conn, now.isoformat())
        self._conn.commit()
        return {"deleted": deleted_count, "reclaimed_bytes": reclaimed_bytes}

    def _delete_one(
        self,
        row: DeletionRow,
        allowed_roots: list[str],
        now_iso: str,
    ) -> list[DeletionRow]:
        """Delete a single eligible row from disk; return the rows closed as deleted.

        Parks a row with an empty/NULL ``file_path`` instead of cycling it
        forever between 'deleting' and 'pending': ``delete_path("")``
        always refuses and recovery always resets to pending, so such a row
        would livelock invisibly. Write a permanent-failure audit and drop
        the action so the operator can see it and it cannot re-enter.

        Two-phase delete: the row is claimed as 'deleting' and committed
        BEFORE the file is removed. If we crash between that commit and the
        rm, the next run's recover_stuck_deletions() can inspect the row and
        decide whether the file is still there (reset to pending) or already
        gone (mark deleted).

        Due rows of other items stored at or under the path are claimed in
        the same transaction, since the removal takes their media too, and
        close with this row; they go back to pending if the removal fails, or
        if a keep or clear made after the claim stops it.
        """
        path = row.file_path or ""
        # Read the disk before the claim opens a write transaction, so a hung
        # mount cannot hold the database lock.
        mounted = bool(path.strip()) and (
            os.path.lexists(path) or delete_root_available(path, allowed_roots)
        )
        if not claim_due_deletion(self._conn, row.id, now_iso):
            self._conn.commit()
            logger.info(
                "engine.delete.skipped id=%s media_id=%s — no longer a due deletion",
                row.id,
                row.media_item_id,
            )
            return []

        if self._cancel_if_kept(row, now_iso):
            self._conn.commit()
            return []

        if not path.strip():
            logger.error(
                "engine.delete.empty_path id=%s media_id=%s — scheduled deletion "
                "has no file_path; parking the action so it cannot livelock "
                "between 'deleting' and 'pending'.",
                row.id,
                row.media_item_id,
            )
            log_audit(
                self._conn,
                row.media_item_id,
                "delete_failed_permanent",
                f"Permanent delete error: {row.title} — empty file_path, action parked",
            )
            repository.delete_scheduled_action(self._conn, row.id)
            self._conn.commit()
            return []

        if not self._may_remove(row, path, now_iso, mounted=mounted):
            repository.mark_delete_status(self._conn, row.id, "pending")
            self._conn.commit()
            return []
        # A kept item cancelled here no longer counts as due, so the check
        # before the removal finds its media held and leaves the path.
        co_located = [
            other
            for other in claim_due_deletions_under(self._conn, row.media_item_id, path, now_iso)
            if not self._cancel_if_kept(other, now_iso)
        ]
        self._conn.commit()
        logger.info(
            "engine.delete.intent id=%s media_id=%s path=%r also_closes=%s",
            row.id,
            row.media_item_id,
            path,
            [other.id for other in co_located],
        )
        # The mount may have dropped since the check before the claim, and
        # removing a target that is gone does nothing: check its root again.
        mounted = os.path.lexists(path) or delete_root_available(path, allowed_roots)
        # A keep or clear may have landed while the disk was read; read the
        # rows after the disk, so the window left before the removal is small.
        if not self._still_removable(row, co_located, path, now_iso, mounted=mounted):
            for claimed in (row, *co_located):
                repository.mark_delete_status(self._conn, claimed.id, "pending")
            self._conn.commit()
            return []
        if not _delete_file_on_disk(self._conn, row, allowed_roots):
            for other in co_located:
                repository.mark_delete_status(self._conn, other.id, "pending")
            self._conn.commit()
            return []
        closed = [row, *co_located]
        for done in closed:
            _commit_deletion(self._conn, done)
        for done in closed:
            _unmonitor_arr(done, radarr_client=self._radarr, sonarr_client=self._sonarr)
        return closed

    def _cancel_if_kept(self, row: DeletionRow, now_iso: str) -> bool:
        """Cancel a claimed row, logging and auditing why, when a keep the scan honours covers it.

        Cancelled rather than left pending, so it cannot fire later with no fresh notice.
        """
        if not cancel_kept_deletion(self._conn, row.id, now_iso):
            return False
        logger.warning(
            "engine.delete.kept id=%s media_id=%s — the item is kept; cancelling its "
            "scheduled deletion",
            row.id,
            row.media_item_id,
        )
        log_audit(
            self._conn,
            row.media_item_id,
            "delete_refused",
            f"Not deleted: {row.title} — it is kept, so its scheduled deletion is cancelled",
        )
        return True

    def _still_removable(
        self,
        row: DeletionRow,
        co_located: list[DeletionRow],
        path: str,
        now_iso: str,
        *,
        mounted: bool,
    ) -> bool:
        """Return False, logging why, when a keep, clear or unmount since the claim stops the removal.

        The row must still be a claimed ``scheduled_deletion``, no keep the scan
        honours may cover it or a co-located row, and _may_remove must still
        pass, so an item under *path* kept since the claim blocks it as it
        would have before.
        """
        if not is_claimed_deletion(self._conn, row.id):
            logger.warning(
                "engine.delete.kept_after_claim id=%s media_id=%s path=%r — kept or "
                "cleared since the claim; leaving the files",
                row.id,
                row.media_item_id,
                path,
            )
            return False
        # A show keep leaves a claimed row's action as it is; its next claim cancels it.
        kept = [c.id for c in (row, *co_located) if is_kept_deletion(self._conn, c.id, now_iso)]
        if kept:
            logger.warning(
                "engine.delete.kept_after_claim id=%s media_id=%s path=%r kept_rows=%s — "
                "kept since the claim; leaving the files",
                row.id,
                row.media_item_id,
                path,
                kept,
            )
            return False
        return self._may_remove(row, path, now_iso, mounted=mounted)

    def _may_remove(self, row: DeletionRow, path: str, now_iso: str, *, mounted: bool) -> bool:
        """Return False, logging why, when removing *path* now could lose media.

        Before the claim it runs inside the claim's transaction, so the other
        items it reads cannot change before the claim commits; it runs again
        just before the removal. *mounted* says whether the target, or else its
        delete root's content, was on disk; it is read before any transaction
        opens.
        """
        held = fetch_media_held_outside_pass(self._conn, row.media_item_id, path, now_iso)
        if held:
            names = ", ".join(
                f"{h['title']} season {h['season_number']}"
                if h["season_number"] is not None
                else str(h["title"])
                for h in held
            )
            logger.warning(
                "engine.delete.shared_path id=%s path=%r — also holds media not due "
                "for deletion: %s; leaving the row pending",
                row.id,
                path,
                names,
            )
            detail = (
                f"Not deleted: {row.title} — {path} also holds media that is not "
                f"due for deletion ({names})"
            )
            # The refusal repeats every pass until something changes; audit it once.
            if not is_latest_audit(self._conn, row.media_item_id, "delete_refused", detail):
                log_audit(self._conn, row.media_item_id, "delete_refused", detail)
            return False
        if not mounted:
            _log_root_unavailable(row, path)
            return False
        return True
