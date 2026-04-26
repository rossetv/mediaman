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
from datetime import datetime, timezone
from typing import Any, TypedDict

from mediaman.audit import log_audit
from mediaman.scanner import repository
from mediaman.services.infra.storage import delete_path

logger = logging.getLogger("mediaman")


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


class DeletionExecutor:
    """Executes pending deletions whose grace period has elapsed.

    Encapsulates the previously in-engine ``execute_deletions`` loop:
    allowlist read, stuck-state recovery, two-phase on-disk rm, audit
    logging, and best-effort Radarr/Sonarr unmonitor calls.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        dry_run: bool = False,
        sonarr_client: Any = None,
        radarr_client: Any = None,
    ) -> None:
        self._conn = conn
        self._dry_run = dry_run
        self._sonarr = sonarr_client
        self._radarr = radarr_client

    def execute(self) -> DeletionResult:
        """Run the deletion pass.

        Returns a dict with ``deleted`` count and ``reclaimed_bytes``
        total. Always cleans up expired snoozes before returning so
        those items re-enter the scan pipeline on the next run.
        """
        now = datetime.now(timezone.utc)
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

            try:
                delete_path(row["file_path"], allowed_roots=allowed_roots)
            except ValueError as exc:
                logger.error(
                    "Refusing to delete '%s' — path is outside configured delete_allowed_roots: %s",
                    row["file_path"],
                    exc,
                )
                # Roll back the marker so the row is re-examined next run.
                repository.mark_delete_status(self._conn, row["id"], "pending")
                self._conn.commit()
                continue
            except Exception:
                logger.exception(
                    "engine.delete.failed id=%s path=%r — leaving row in "
                    "'deleting' state for recovery on next run",
                    row["id"],
                    row["file_path"],
                )
                continue

            # Record the deletion and close the transaction *before* the
            # Radarr/Sonarr unmonitor HTTP calls. The unmonitor is
            # best-effort housekeeping — a failure (or slow response)
            # must not keep the SQLite write lock open.
            rk = row["plex_rating_key"]
            detail = f"Deleted: {row['title']}" + (f" [rk:{rk}]" if rk else "")
            log_audit(
                self._conn,
                row["media_item_id"],
                "deleted",
                detail,
                space_bytes=row["file_size_bytes"],
            )
            repository.delete_scheduled_action(self._conn, row["id"])
            self._conn.commit()

            # Unmonitor in *arr clients — failures are non-fatal and
            # happen outside any open transaction.
            if row["radarr_id"] and self._radarr:
                try:
                    self._radarr.unmonitor_movie(row["radarr_id"])
                except Exception:
                    logger.warning(
                        "Failed to unmonitor movie %s after deletion",
                        row["radarr_id"],
                        exc_info=True,
                    )

            if row["sonarr_id"] and row["season_number"] is not None and self._sonarr:
                try:
                    self._sonarr.unmonitor_season(row["sonarr_id"], row["season_number"])
                except Exception:
                    logger.warning(
                        "Failed to unmonitor season %s of series %s after deletion",
                        row["season_number"],
                        row["sonarr_id"],
                        exc_info=True,
                    )

            deleted_count += 1
            reclaimed_bytes += row["file_size_bytes"] or 0

        # Remove expired snoozes so items re-enter the scan pipeline.
        repository.cleanup_expired_snoozes(self._conn, now.isoformat())

        self._conn.commit()
        return {"deleted": deleted_count, "reclaimed_bytes": reclaimed_bytes}
