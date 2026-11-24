"""Deletion executor — removes files and reconciles the DB.

All deletion-time concerns live here:

* Stuck-state recovery (recover_stuck_deletions) for rows left in
  the ``deleting`` state by a previous crash between the on-disk rm and
  the DB cleanup commit.
* Allowlist enforcement via repository.read_delete_allowed_roots_setting.
* The two-phase delete loop that unmonitors *arr clients only after the
  SQLite write lock has been released, and only an entry that matches.
* The record of which delete roots or folders the last pass found
  missing or empty, for the dashboard.

Imports repository; MUST NOT import fetch or
engine (see engine.py header).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import requests

from mediaman.core.audit import log_audit
from mediaman.core.delete_root_state import read_delete_root_state, write_delete_root_state
from mediaman.core.time import now_utc
from mediaman.scanner import repository
from mediaman.scanner.repository import DeletionRow
from mediaman.scanner.repository.scheduled_actions import (
    DELETION_ACTION,
    book_deletion,
    book_removed_files,
    cancel_kept_deletion,
    claim_due_deletion,
    claim_due_deletions_under,
    fetch_deletion,
    fetch_media_held_outside_pass,
    fetch_pending_unmonitors,
    fetch_tracked_paths_in_folders,
    finish_unmonitor,
    is_claimed_deletion,
    is_fully_removed,
    is_kept_deletion,
    is_latest_audit,
    record_removed_files,
    tracked_paths,
    was_requested_since,
)
from mediaman.services.arr.base import ArrError
from mediaman.services.arr.match import build_arr_index, index_match_movie, index_match_season
from mediaman.services.infra import (
    DeletionRefused,
    SafeHTTPError,
    TrackedFilesResult,
    delete_tracked_files,
    path_within_delete_roots,
    remove_empty_folders,
)
from mediaman.services.infra.storage import unavailable_folder

if TYPE_CHECKING:
    from datetime import datetime

    from mediaman.services.arr.base import ArrClient

logger = logging.getLogger(__name__)

_RECORD_ATTEMPTS = 3


class DeletionResult(TypedDict):
    deleted: int
    reclaimed_bytes: int


_SEASON_FOLDER = re.compile(r"(?:season|series|s)[ ._-]*(\d+)", re.IGNORECASE)


def _season_of_folder(folder: str) -> int | None:
    """Return the season number *folder*'s name plainly gives, else None."""
    name = os.path.basename(folder)
    if name.lower() == "specials":
        return 0
    match = _SEASON_FOLDER.fullmatch(name)
    return int(match.group(1)) if match else None


def _folder_limit(row: DeletionRow, path: str) -> str:
    """Return the highest folder a bare-folder walk from tracked file *path* may remove.

    A film's own folder; for a season, the series folder above a season
    folder, or the folder holding the episodes when it is not plainly a
    season folder (a show stored without season folders), so the walk never
    climbs above the show. A parent named like a season folder too may be a
    show stored without season folders, so the walk stays below it. The
    storage layer refuses a limit that is a delete root or above one.
    """
    folder = os.path.dirname(path)
    parent = os.path.dirname(folder)
    if (
        row.media_type != "movie"
        and row.season_number is not None
        and _season_of_folder(folder) == row.season_number
        and _season_of_folder(parent) is None
    ):
        return parent
    return folder


def _first_unavailable(paths: list[str], allowed_roots: list[str]) -> str | None:
    """Return the folder found missing or empty above any of *paths*, else None."""
    # One check per folder: files beside each other share the answer.
    for path in {os.path.dirname(path): path for path in paths}.values():
        if (folder := unavailable_folder(path, allowed_roots)) is not None:
            return folder
    return None


def recover_stuck_deletions(conn: sqlite3.Connection, *, book: bool = True) -> list[DeletionRow]:
    """Reconcile ``scheduled_actions`` rows left in the ``deleting`` state.

    Called at the start of a *live* DeletionExecutor.execute pass
    (never under dry-run — a preview mutates nothing) and by the
    scheduler on startup. For each row marked ``deleting`` we check
    whether the item's tracked files are still on disk:

    * No tracked files recorded -> nothing proves the files are gone. Reset
      to ``pending``; the live pass then waits for a scan to record them.
    * Every tracked file absent, but one not recorded as removed by an
      earlier pass of this deletion -> nothing proves mediaman removed it
      (it may only have moved). Reset to ``pending`` and audit once that it
      waits for a scan, as the live pass does.
    * Every tracked file absent and recorded as removed, **and** each stored
      path was within the configured delete roots -> the removal completed
      but the follow-up bookkeeping was never committed. Convert to a normal
      ``deleted`` cleanup: write the audit entry (crediting the recorded
      bytes) and drop the row. Folders left holding only artwork do not
      count as files. If a keep or snooze was written over the row during
      the removal, the keep stands: audit that the media is gone and reset
      to ``pending``; the live pass then books what was removed.
    * Files absent **but** a stored path was NOT within the configured
      delete roots (an operator changed ``MEDIAMAN_DELETE_ROOTS`` between
      the crash and restart, or the path was always bogus) -> mediaman
      could never have deleted it, so we must NOT fabricate a "deleted N
      bytes" audit row (that would corrupt the audit trail and the
      reclaimed-bytes stats — a security surface). Reset to ``pending``
      and log loudly instead.
    * Files absent **but** their delete root is missing or empty -> the
      media mount is likely down, so the absence proves nothing. Reset to
      ``pending``. Only files not recorded as removed are looked at.
    * Any tracked file present -> the removal never ran or did not finish.
      Reset to ``pending`` so the next normal run retries cleanly.

    With *book* False a row that would be booked goes back to ``pending``
    with its record kept, so the next live pass books it and unmonitors it
    in Radarr/Sonarr; the scheduler recovers this way at startup, with no
    clients to unmonitor through.

    Idempotent; safe to call on every startup. Does not itself delete
    any files — purely a state reconciliation. Each row is committed before
    the next row's disk checks, so a hung mount never holds the write lock.
    Returns the rows booked as deleted.
    """
    booked: list[DeletionRow] = []
    rows = repository.fetch_stuck_deletions(conn)
    if not rows:
        return booked

    # Read the allowlist once so the file-absent branch can confirm the
    # stored paths were ever deletable before crediting freed bytes.
    allowed_roots = repository.read_delete_allowed_roots_setting(conn)

    for row in rows:
        paths = tracked_paths(row)
        if paths is None:
            logger.warning(
                "engine.delete.recover id=%s — no tracked files recorded, reverting "
                "status to 'pending'",
                row.id,
            )
            repository.mark_delete_status(conn, row.id, "pending")
        elif any(os.path.lexists(path) for path in paths):
            logger.warning(
                "engine.delete.recover id=%s path=%r — tracked files still present, "
                "reverting status to 'pending'",
                row.id,
                row.file_path,
            )
            repository.mark_delete_status(conn, row.id, "pending")
        elif not all(path_within_delete_roots(path, allowed_roots) for path in paths):
            # Files are gone but a path was never inside an allowed root —
            # mediaman did not (and could not) delete it. Do NOT write a
            # phantom "deleted" audit row. Reset to pending and shout.
            logger.error(
                "engine.delete.recover id=%s path=%r — files absent but a tracked path "
                "is outside the configured delete_allowed_roots (or roots are "
                "unconfigured). Refusing to fabricate a 'deleted' audit entry; "
                "reverting status to 'pending' for operator review.",
                row.id,
                row.file_path,
            )
            repository.mark_delete_status(conn, row.id, "pending")
        elif (
            unavailable := _first_unavailable(
                # A file this deletion removed is expected to be gone, and the
                # folder walk after it may have emptied the folder above.
                [path for path in paths if str(Path(path)) not in row.removed_paths],
                allowed_roots,
            )
        ) is not None:
            logger.error(
                "engine.delete.recover id=%s path=%r — files absent but their delete "
                "root is missing or empty (media mount down?); reverting status "
                "to 'pending' instead of booking it as deleted.",
                row.id,
                row.file_path,
            )
            _audit_root_unavailable(conn, row, unavailable)
            repository.mark_delete_status(conn, row.id, "pending")
        elif not {str(Path(path)) for path in paths} <= row.removed_paths:
            logger.warning(
                "engine.delete.recover id=%s path=%r — files gone, but this deletion did not "
                "record removing them all; reverting status to 'pending' until a scan "
                "records where they are",
                row.id,
                row.file_path,
            )
            _audit_once(
                conn,
                row,
                "delete_awaiting_scan",
                f"Not deleted yet: {row.title} — its files are gone, but mediaman did not "
                "record removing them; the deletion waits for the next scan to record "
                "where they are",
            )
            repository.mark_delete_status(conn, row.id, "pending")
        elif row.action != DELETION_ACTION:
            logger.warning(
                "engine.delete.recover id=%s path=%r action=%r — files gone, but the "
                "item was kept during its removal; keeping the row",
                row.id,
                row.file_path,
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
        elif not book:
            logger.warning(
                "engine.delete.recover id=%s path=%r — files already gone; reverting status "
                "to 'pending' for the next live pass to book",
                row.id,
                row.file_path,
            )
            repository.mark_delete_status(conn, row.id, "pending")
        else:
            logger.warning(
                "engine.delete.recover id=%s path=%r — files already gone, completing cleanup",
                row.id,
                row.file_path,
            )
            book_deletion(
                conn, row, "Deleted (recovered)", row.removed_bytes or None, unmonitor=True
            )
            booked.append(row)
        conn.commit()
    return booked


# rationale: four distinct exception types (DeletionRefused, FileNotFoundError,
# PermissionError/OSError, Exception) each require different recovery
# actions; splitting by exception type would separate try from except.
def _delete_files_on_disk(
    conn: sqlite3.Connection,
    row: DeletionRow,
    tracked_files: list[dict[str, Any]],
    allowed_roots: list[str],
    protected_paths: set[str],
    removed_paths: set[str],
) -> TrackedFilesResult | None:
    """Remove *tracked_files* with their sidecars.

    A tracked file in *removed_paths*, already removed by an earlier pass,
    still has its sidecars swept, so one that failed to go then goes now.

    Returns what was removed, or None on an error: the row is then left in
    the appropriate state so the caller can ``continue`` to the next row. A failed removal
    goes back to ``pending``: the pass reads its rows once, so the next pass
    retries it. ``deleting`` means a removal running now, or a leftover whose
    files are already gone (missing at the rm, or a crash) awaiting recovery.  All
    try/except branches are contained here so no except clause is ever
    stranded in a different scope from its try.
    """
    try:
        result = delete_tracked_files(
            tracked_files,
            allowed_roots=allowed_roots,
            protected_paths=protected_paths,
            sweep_sidecars_of=removed_paths,
        )
    except DeletionRefused as exc:
        # Safety refusal: the path or its mount is wrong, but the
        # action row is still valid — reset to pending so a later
        # run (e.g. once the operator fixes the path) can retry.
        logger.error("Refusing to delete '%s': %s", row.file_path, exc)
        repository.mark_delete_status(conn, row.id, "pending")
        detail = f"Not deleted: {row.title} — {exc}"
        # The refusal repeats every pass until something changes; audit it once.
        if not is_latest_audit(conn, row.media_item_id, "delete_refused", detail):
            log_audit(conn, row.media_item_id, "delete_refused", detail)
        conn.commit()
        return None
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
        return None
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
        return None
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
        return None
    return result


def _remove_bare_folders(
    conn: sqlite3.Connection,
    row: DeletionRow,
    removed: list[str],
    allowed_roots: list[str],
    folder_limits: dict[str, str],
) -> None:
    """Remove the folders *removed* left bare; a failure is logged and audited, never raised."""
    # A sidecar shares its video's folder, so it shares the limit too.
    boundaries = {
        path: limit
        for path in removed
        if (limit := folder_limits.get(os.path.dirname(path))) is not None
    }
    try:
        remove_empty_folders(removed, allowed_roots=allowed_roots, boundaries=boundaries)
    # rationale: the files are already gone, so whatever goes wrong here must
    # not stop the pass booking them.
    except Exception as exc:
        logger.exception(
            "engine.delete.folders_failed id=%s path=%r — files removed, but removing the "
            "folders they left bare failed",
            row.id,
            row.file_path,
        )
        _audit_once(
            conn,
            row,
            "delete_folders_failed",
            f"Folders left: {row.title} — its files were removed, but the folders they left "
            f"bare could not be removed ({exc.__class__.__name__}: {exc})",
        )


_ARR_ERRORS = (SafeHTTPError, requests.RequestException, ArrError, ValueError)


def _is_not_found(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 404:
        return True
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def _arr_file_key(arr_file: object) -> tuple[str, int] | None:
    """Return ``(name, size)`` of a Radarr/Sonarr file payload, or None when it says neither."""
    if not isinstance(arr_file, Mapping):
        return None
    path = arr_file.get("relativePath") or arr_file.get("path")
    name = re.split(r"[\\/]", path)[-1] if isinstance(path, str) else ""
    size = arr_file.get("size")
    if not name or not isinstance(size, int) or isinstance(size, bool):
        return None
    return name, size


def _tracked_sizes(row: DeletionRow) -> dict[str, int]:
    """Return the size the scan recorded for each of *row*'s tracked files that has one."""
    sizes = {}
    for entry in row.tracked_files or []:
        path, size = entry.get("path"), entry.get("size")
        if isinstance(path, str) and isinstance(size, int) and not isinstance(size, bool):
            sizes[str(Path(path))] = size
    return sizes


def _removed_file_keys(row: DeletionRow) -> set[tuple[str, int]]:
    """Return ``(name, size)`` of each file *row*'s deletion removed.

    Only a booked row is unmonitored, and a row is booked only once every
    tracked file of it is gone by this deletion's hand, so its tracked files
    were removed. So were the files its record holds, which a scan between
    passes may have dropped from the tracked files; one recorded without a
    size matches nothing.
    """
    sizes = {**row.removed_sizes, **_tracked_sizes(row)}
    return {(os.path.basename(path), size) for path, size in sizes.items()}


def _lookup_entry(
    stored_id: int | None,
    get_by_id: Callable[[int], Any],
    get_by_external_id: Callable[[int], Any],
    external_id: int,
) -> Any:
    """Return the arr's entry for an item, or None when it has none.

    An entry deleted in the arr and added back has a new id, so a 404 on the
    stored id falls back to the TMDB/TVDB lookup. That lookup answers no match
    with an empty list, so its own 404 is an error, raised like any other.
    """
    if stored_id:
        try:
            return get_by_id(stored_id)
        except _ARR_ERRORS as exc:
            if not _is_not_found(exc):
                raise
    return get_by_external_id(external_id)


def _no_entry(is_movie: bool) -> str:
    return "Radarr has no entry for this movie" if is_movie else "Sonarr has no entry for this show"


def _unmonitor_movie(row: DeletionRow, client: ArrClient) -> str | None:
    """Unmonitor the Radarr entry matching *row*; return why not when it does not match."""
    tmdb_id = row.tmdb_id
    if not tmdb_id:
        return "Plex has no TMDB id for this movie"
    entry = _lookup_entry(row.radarr_id, client.get_movie_by_id, client.get_movie_by_tmdb, tmdb_id)
    if not entry or not isinstance(entry.get("id"), int):
        return _no_entry(True)
    if entry.get("tmdbId") != tmdb_id:
        return "Radarr's entry is a different movie (TMDB id differs)"
    index = build_arr_index([entry], [])
    radarr_id = next(
        (
            found
            for path in tracked_paths(row) or []
            if (found := index_match_movie(index, tmdb_id=tmdb_id, plex_file_path=path)) is not None
        ),
        None,
    )
    if radarr_id is None:
        return "Radarr's folder for this movie is not the one Plex used"
    # A second copy (a 4K library, say) can share the id and the folder name;
    # unmonitoring its entry would stop Radarr upgrading the copy that was kept.
    movie_file = entry.get("movieFile")
    if movie_file is not None or entry.get("hasFile"):
        key = _arr_file_key(movie_file)
        if key is None or key not in _removed_file_keys(row):
            return "Radarr's file for this movie is not one mediaman deleted"
    client.unmonitor_movie(radarr_id)
    return None


def _unmonitor_season(row: DeletionRow, client: ArrClient) -> str | None:
    """Unmonitor the Sonarr season matching *row*; return why not when it does not match."""
    tvdb_id = row.tvdb_id
    if not tvdb_id:
        return "Plex has no TVDB id for this show"
    season = row.season_number
    if season is None:
        return "mediaman does not know this season's number"
    series = _lookup_entry(
        row.sonarr_id, client.get_series_by_id, client.get_series_by_tvdb, tvdb_id
    )
    if not series or not isinstance(series.get("id"), int):
        return _no_entry(False)
    if series.get("tvdbId") != tvdb_id:
        return "Sonarr's entry is a different show (TVDB id differs)"
    index = build_arr_index([], [series])
    ref = index.series.get(tvdb_id)
    if ref is None or season not in ref.seasons:
        return "Sonarr does not have this season"
    sonarr_id = next(
        (
            found
            for folder in {os.path.dirname(path) for path in tracked_paths(row) or []}
            if (
                found := index_match_season(
                    index, tvdb_id=tvdb_id, season_number=season, plex_file_path=folder
                )
            )
            is not None
        ),
        None,
    )
    if sonarr_id is None:
        return "Sonarr's folder for this show is not the one Plex used"
    # Sonarr still lists the removed files until its own rescan; any other
    # file in the season is a copy that was kept.
    removed = _removed_file_keys(row)
    for episode_file in client.get_episode_files(sonarr_id):
        if (
            isinstance(episode_file, Mapping)
            and episode_file.get("seasonNumber") == season
            and _arr_file_key(episode_file) not in removed
        ):
            return "Sonarr has a file in this season that mediaman did not delete"
    client.unmonitor_season(sonarr_id, season)
    return None


def _unmonitor(
    row: DeletionRow, *, radarr_client: ArrClient | None, sonarr_client: ArrClient | None
) -> str | None:
    """Stop Radarr/Sonarr re-downloading a deleted item; return why not, when that must be told.

    Only ever unmonitors, after checking it is the same item: the files are
    already gone, so a wrong match could at worst stop a download, never delete
    anything. With no client for the item's kind, or no entry for the item in
    it, there is nothing to unmonitor.
    """
    is_movie = row.media_type == "movie"
    service = "Radarr" if is_movie else "Sonarr"
    client = radarr_client if is_movie else sonarr_client
    if client is None:
        return None
    try:
        reason = _unmonitor_movie(row, client) if is_movie else _unmonitor_season(row, client)
    except _ARR_ERRORS:
        logger.warning("Unmonitor in %s failed for %r", service, row.title, exc_info=True)
        reason = f"{service} could not be reached or returned an error"
    if reason is None:
        return None
    if reason == _no_entry(is_movie):
        # Nothing to unmonitor, and nothing it could download again.
        logger.info("engine.delete.unmonitor_none media_id=%s — %s", row.media_item_id, reason)
        return None
    logger.warning("engine.delete.unmonitor_skipped media_id=%s — %s", row.media_item_id, reason)
    return (
        f"Not unmonitored in {service}: {row.title} — {reason}; it was deleted, but "
        f"{service} may download it again"
    )


def _came_back(conn: sqlite3.Connection, row: DeletionRow) -> bool:
    """Return True if a booked deletion's title was asked for again, or is on disk again."""
    paths = row.removed_paths.union(tracked_paths(row) or [])
    return was_requested_since(conn, row) or any(os.path.lexists(path) for path in paths)


def unmonitor_deleted(
    conn: sqlite3.Connection,
    *,
    radarr_client: ArrClient | None,
    sonarr_client: ArrClient | None,
) -> None:
    """Unmonitor in Radarr/Sonarr every booked deletion still marked for it.

    Runs after the deletion committed, so a slow response never holds the write
    lock, and commits each item's outcome as it goes: one a dead process left
    marked is done by the next call. It is tried once; a skip is audited as
    ``unmonitor_skipped`` and the deletion stays booked either way. A title
    that came back meanwhile is left monitored.
    """
    for row in fetch_pending_unmonitors(conn):
        if _came_back(conn, row):
            logger.info("engine.delete.unmonitor_over media_id=%s", row.media_item_id)
            detail = None
        else:
            detail = _unmonitor(row, radarr_client=radarr_client, sonarr_client=sonarr_client)
        with conn:
            if finish_unmonitor(conn, row.id) and detail is not None:
                log_audit(conn, row.media_item_id, "unmonitor_skipped", detail)


def _unavailable(path: str, allowed_roots: list[str]) -> str | None:
    """Name the folder found missing or empty when the target is gone, else None.

    None too for a path outside the delete roots: its mount is not the cause,
    and the removal refuses it with that reason.
    """
    if os.path.lexists(path) or not path_within_delete_roots(path, allowed_roots):
        return None
    return unavailable_folder(path, allowed_roots)


def _audit_once(
    conn: sqlite3.Connection, row: DeletionRow, action: str, detail: str, **kw: Any
) -> None:
    # A hold repeats every pass until something changes; audit it once.
    if not is_latest_audit(conn, row.media_item_id, action, detail):
        log_audit(conn, row.media_item_id, action, detail, **kw)


def _audit_files_unrecorded(conn: sqlite3.Connection, row: DeletionRow) -> None:
    _audit_once(
        conn,
        row,
        "delete_awaiting_scan",
        f"Not deleted yet: {row.title} — its files are not recorded yet; the deletion "
        "waits for the next scan to record them",
    )


def _files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def _audit_files_left(conn: sqlite3.Connection, row: DeletionRow, left: _FilesLeft) -> None:
    parts = []
    if left.not_found:
        parts.append(f"{_files(left.not_found)} not found at stored path")
    if left.protected:
        parts.append(f"{_files(left.protected)} kept because another item tracks them")
    if left.failed:
        parts.append(f"{_files(len(left.failed))} could not be removed ({left.failed[0]})")
    then = (
        "the deletion waits for the next scan to record where they are"
        if left.not_found
        else "the next pass tries again"
    )
    _audit_once(
        conn,
        row,
        "delete_incomplete",
        f"Not deleted yet: {row.title} — {'; '.join(parts)}; {then}",
    )


@dataclass(frozen=True)
class _FilesLeft:
    """The files of one item a removal left on disk, by why."""

    not_found: int
    protected: int
    failed: list[str]

    def __bool__(self) -> bool:
        return bool(self.not_found or self.protected or self.failed)


def _files_left(row: DeletionRow, result: TrackedFilesResult, tracked: set[str]) -> _FilesLeft:
    """Return what *result* left of *row*'s files; *tracked* holds every closing row's paths.

    A failed sidecar counts against each row with a tracked file in its folder.
    A file an earlier pass of this deletion removed is not left, though not found.
    """
    own = {str(Path(path)) for path in tracked_paths(row) or []}
    folders = {os.path.dirname(path) for path in own}
    missing = own - row.removed_paths
    return _FilesLeft(
        not_found=sum(1 for path in result.not_found if str(Path(path)) in missing),
        protected=sum(1 for path in result.skipped_protected if str(Path(path)) in own),
        failed=[
            reason
            for path, reason in result.failed
            if str(Path(path)) in own
            or (str(Path(path)) not in tracked and os.path.dirname(path) in folders)
        ],
    )


def _own_removed(row: DeletionRow, result: TrackedFilesResult) -> set[str]:
    """Return *row*'s tracked files that *result* removed."""
    removed = {str(Path(path)) for path in result.removed}
    return {str(Path(path)) for path in tracked_paths(row) or []} & removed


def _unavailable_for(row: DeletionRow, allowed_roots: list[str]) -> str | None:
    """Name the folder found missing or empty above *row*'s files, else None.

    The tracked files' folders decide; ``file_path`` stands in until a scan
    records the files. A file an earlier pass of this deletion removed is
    expected to be gone, so it says nothing about the mount.
    """
    stored = row.file_path or ""
    tracked = tracked_paths(row)
    if tracked is not None:
        paths = [path for path in tracked if str(Path(path)) not in row.removed_paths]
    else:
        paths = [stored] if stored.strip() else []
    # One check per folder: files beside each other share the answer.
    for path in {os.path.dirname(path): path for path in paths}.values():
        if (folder := _unavailable(path, allowed_roots)) is not None:
            return folder
    return None


def _new_copy(row: DeletionRow) -> bool:
    """Return True when the disk holds a copy of *row*'s item that came after its removal began.

    A file this deletion removed is there again, or its item tracks a file
    that was not among those still to remove.
    """
    if any(os.path.lexists(path) for path in row.removed_paths):
        return True
    if not row.removed_paths or row.left_paths is None:
        return False
    known = row.removed_paths | row.left_paths
    return any(
        str(Path(path)) not in known and os.path.lexists(path) for path in tracked_paths(row) or []
    )


# Audit rows about a folder rather than an item carry this prefix and the
# folder, so each folder's row is deduped on its own.
_FOLDER_AUDIT_ID = "_delete_root:"


def _record_folders_unavailable(conn: sqlite3.Connection, waiting: dict[str, list[int]]) -> None:
    """Store *waiting* (folder -> ids of the rows held back) and audit how it differs from the last pass.

    A folder newly held, or holding a different count, gets a row; a folder no
    longer held gets one saying nothing waits on it any more, which does not
    mean it has content: its last waiting row may simply have gone.
    """
    previous = read_delete_root_state(conn)
    write_delete_root_state(conn, {f: len(ids) for f, ids in waiting.items()}, waiting)
    for folder, ids in waiting.items():
        count = len(ids)
        if previous.get(folder) == count:
            continue
        items = "1 scheduled deletion waits" if count == 1 else f"{count} scheduled deletions wait"
        log_audit(
            conn,
            _FOLDER_AUDIT_ID + folder,
            "delete_root_unavailable",
            f"Delete root or folder '{folder}' is missing or empty, so the media mount may "
            f"be down; {items} until it has content again",
        )
    for folder in previous.keys() - waiting.keys():
        log_audit(
            conn,
            _FOLDER_AUDIT_ID + folder,
            "delete_root_available",
            f"Delete root or folder '{folder}': no scheduled deletions wait on it any more",
        )


def _audit_root_unavailable(conn: sqlite3.Connection, row: DeletionRow, folder: str) -> None:
    detail = (
        f"Not deleted yet: {row.title} — {folder} is missing or empty, so the media "
        "mount may be down; the deletion waits until that folder has content again"
    )
    _audit_once(conn, row, "delete_root_unavailable", detail)


class DeletionExecutor:
    """Executes pending deletions whose grace period has elapsed.

    Runs the deletion loop:
    allowlist read, stuck-state recovery, two-phase on-disk rm, audit
    logging, best-effort Radarr/Sonarr unmonitor calls after a match, and
    the record of delete roots or folders found missing or empty.

    Args:
        conn: Open SQLite connection.
        dry_run: When True, skip the on-disk rm (and the row deletion +
            audit-log entry that would normally follow). A
            ``dry_run_skip`` audit row is written instead so an operator
            can see what *would* have been deleted; the record of roots or
            folders found missing or empty is still refreshed. Snooze cleanup is
            controlled separately via ``cleanup_snoozes``.
        cleanup_snoozes: When True (default), expired snoozes are
            removed so the items re-enter the scan pipeline. Set to
            False when the caller is performing a true dry-run preview
            and must not mutate ``scheduled_actions``.
        sonarr_client: Optional Sonarr API client for unmonitor calls.
        radarr_client: Optional Radarr API client for unmonitor calls.
            Neither is ever asked to delete files.
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
        # Action row id -> the folder found missing or empty that holds it back.
        self._waiting: dict[int, str] = {}

    def execute(self) -> DeletionResult:
        """Run the deletion pass.

        Returns a dict with ``deleted`` count and ``reclaimed_bytes``
        total. Cleans up expired snoozes before returning unless
        ``cleanup_snoozes`` was set to ``False`` at construction, ensuring
        a real dry-run preview never mutates ``scheduled_actions``.

        Pipeline:
        1. Recover stuck rows (skipped in dry-run — preview mutates nothing).
        2. Dry-run: write a ``dry_run_skip`` audit per pending row not kept,
           record which roots or folders would hold rows back, and stop.
        3. Live: load allowlist (fail closed if unset), then for each row
           mark 'deleting', delete its tracked files, commit audit + unmonitor.
        4. Live: record which roots or folders held rows back, for the dashboard.
        5. Clean up expired snoozes unless dry-run.
        """
        now = now_utc()
        rows = self._recover_then_fetch_pending(now)
        if self._dry_run:
            self._preview(rows, now)
            return {"deleted": 0, "reclaimed_bytes": 0}
        return self._execute_live(rows, now)

    def _preview(self, rows: list[DeletionRow], now: datetime) -> None:
        """Record what *would* be deleted and which folders would hold rows back.

        Mutates no deletion: only ``dry_run_skip`` rows and the record of
        unavailable folders, which the dashboard would otherwise keep showing
        from the last live pass however long the passes stay dry runs.
        """
        allowed_roots = repository.read_delete_allowed_roots_setting(self._conn)
        due = [
            row
            for row in rows
            if row.library_selected and not is_kept_deletion(self._conn, row.id, now.isoformat())
        ]
        # Read the disk before writing anything, so a hung mount cannot hold the lock.
        waiting: dict[str, list[int]] = {}
        for row in due:
            if allowed_roots and (folder := _unavailable_for(row, allowed_roots)) is not None:
                waiting.setdefault(folder, []).append(row.id)
        for row in due:
            log_audit(self._conn, row.media_item_id, "dry_run_skip", f"Would delete: {row.title}")
        _record_folders_unavailable(self._conn, waiting)
        self._conn.commit()

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
            self._unmonitor()
        return repository.fetch_pending_deletions(self._conn, now.isoformat())

    def _unmonitor(self) -> None:
        unmonitor_deleted(self._conn, radarr_client=self._radarr, sonarr_client=self._sonarr)

    def _execute_live(self, rows: list[DeletionRow], now: datetime) -> DeletionResult:
        """Delete each eligible row from disk, fail-closed on an empty allowlist."""
        deleted_count = 0
        reclaimed_bytes = 0
        self._waiting = {}
        book_removed_files(self._conn)
        self._conn.commit()
        allowed_roots = repository.read_delete_allowed_roots_setting(self._conn)
        # Fail closed once, before the loop: deletion must refuse when
        # MEDIAMAN_DELETE_ROOTS is unset. Checked outside the per-row
        # loop so the refusal is logged a single time, not once per pending
        # row. Snooze cleanup below still runs so expired snoozes
        # re-enter the pipeline once roots are configured.
        if not allowed_roots:
            if rows:
                logger.error(
                    "Skipping %d pending deletion(s): no delete folders are configured. "
                    "Set the MEDIAMAN_DELETE_ROOTS env var or the delete_allowed_roots setting.",
                    len(rows),
                )
        else:
            for row in self._book_over(rows, now.isoformat()):
                closed, freed = self._delete_one(row, allowed_roots, now.isoformat())
                deleted_count += len(closed)
                reclaimed_bytes += freed
        by_folder: dict[str, list[int]] = {}
        for row_id, folder in self._waiting.items():
            by_folder.setdefault(folder, []).append(row_id)
        _record_folders_unavailable(self._conn, by_folder)
        if self._cleanup_snoozes:
            repository.cleanup_expired_snoozes(self._conn, now.isoformat())
        self._conn.commit()
        return {"deleted": deleted_count, "reclaimed_bytes": reclaimed_bytes}

    def _book_over(self, rows: list[DeletionRow], now_iso: str) -> list[DeletionRow]:
        """Book each row with nothing to remove any more; return the other rows.

        That is an item with a new copy on disk: the notice was for the copy
        already removed, so the new one starts afresh, and Radarr/Sonarr keep
        monitoring it. Or an item whose files are all removed, which the
        removal could not close. Runs over every row first, as a row may
        close with another's.
        """
        others = []
        for row in rows:
            replaced = _new_copy(row)
            if not replaced and not self._cannot_close(row, now_iso):
                others.append(row)
                continue
            booked = claim_due_deletion(self._conn, row.id, now_iso) and not self._cancel_if_kept(
                row, now_iso
            )
            if booked:
                logger.warning(
                    "engine.delete.over id=%s media_id=%s path=%r new_copy=%s — booking what "
                    "was removed; removing nothing more",
                    row.id,
                    row.media_item_id,
                    row.file_path,
                    replaced,
                )
                if replaced:
                    log_audit(
                        self._conn,
                        row.media_item_id,
                        "delete_refused",
                        f"Not deleted: {row.title} — a new copy is on disk, so only the removal "
                        "of the old one is recorded",
                    )
                book_deletion(
                    self._conn,
                    row,
                    "Deleted (recovered)",
                    row.removed_bytes or None,
                    unmonitor=not replaced,
                )
            self._conn.commit()
            if booked:
                self._unmonitor()
        return others

    def _cannot_close(self, row: DeletionRow, now_iso: str) -> bool:
        """Return True for a fully removed row the removal would hold back or drop."""
        path = row.file_path or ""
        return is_fully_removed(row) and (
            not row.library_selected
            or tracked_paths(row) is None
            or not path.strip()
            or bool(fetch_media_held_outside_pass(self._conn, row.media_item_id, path, now_iso))
        )

    def _delete_one(
        self,
        row: DeletionRow,
        allowed_roots: list[str],
        now_iso: str,
    ) -> tuple[list[DeletionRow], int]:
        """Delete a single eligible row's files; return the rows closed as deleted and bytes freed.

        Parks a row with an empty/NULL ``file_path`` instead of cycling it
        forever between 'deleting' and 'pending': with no path the checks for
        other media held under it cannot run, so such a row would livelock
        invisibly. Write a permanent-failure audit and drop
        the action so the operator can see it and it cannot re-enter.

        Two-phase delete: the row is claimed as 'deleting' and committed
        BEFORE the file is removed. If we crash between that commit and the
        rm, the next run's recover_stuck_deletions() can inspect the row and
        decide whether the file is still there (reset to pending) or already
        gone (mark deleted).

        Only the tracked files go, with their sidecars and the folders left
        bare. A row whose files are not recorded yet waits for a scan, and a
        row with a tracked file missing (moved since the scan) stays pending:
        it is never booked deleted while its media may still be elsewhere.

        Due rows of other items stored at or under the path are claimed in
        the same transaction and their tracked files removed with this row's;
        each closes once all its own files are gone. They go back to pending
        if the removal fails, or if a keep or clear made after the claim stops it.
        """
        # Read the disk before the claim opens a write transaction, so a hung
        # mount cannot hold the database lock.
        unavailable = _unavailable_for(row, allowed_roots)
        if not claim_due_deletion(self._conn, row.id, now_iso):
            self._conn.commit()
            logger.info(
                "engine.delete.skipped id=%s media_id=%s — no longer a due deletion",
                row.id,
                row.media_item_id,
            )
            return [], 0

        # A row that closed with another's removal may hold a newer record.
        row = fetch_deletion(self._conn, row.id) or row
        if self._cancel_if_kept(row, now_iso):
            self._conn.commit()
            return [], 0

        if not row.library_selected:
            logger.info(
                "engine.delete.library_unselected id=%s media_id=%s — its library is not "
                "selected; leaving the row pending",
                row.id,
                row.media_item_id,
            )
            _audit_once(
                self._conn,
                row,
                "delete_refused",
                f"Not deleted yet: {row.title} — its library is not selected in Settings",
            )
            repository.mark_delete_status(self._conn, row.id, "pending")
            self._conn.commit()
            return [], 0

        path = row.file_path or ""

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
            return [], 0

        if tracked_paths(row) is None:
            logger.warning(
                "engine.delete.unrecorded id=%s media_id=%s — the item's files are not "
                "recorded yet; leaving the row pending until a scan records them",
                row.id,
                row.media_item_id,
            )
            repository.mark_delete_status(self._conn, row.id, "pending")
            _audit_files_unrecorded(self._conn, row)
            if unavailable is not None:
                self._waiting[row.id] = unavailable
            self._conn.commit()
            return [], 0

        if not self._may_remove(row, path, now_iso, unavailable=unavailable):
            repository.mark_delete_status(self._conn, row.id, "pending")
            self._conn.commit()
            return [], 0
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
        unavailable = _unavailable_for(row, allowed_roots)
        replaced = [claimed.id for claimed in (row, *co_located) if _new_copy(claimed)]
        if replaced:
            logger.warning(
                "engine.delete.new_copy_after_claim id=%s path=%r replaced_rows=%s — a new copy "
                "is on disk; leaving the files",
                row.id,
                path,
                replaced,
            )
        # A keep or clear may have landed while the disk was read; read the
        # rows after the disk, so the window left before the removal is small.
        if replaced or not self._still_removable(
            row, co_located, path, now_iso, unavailable=unavailable
        ):
            for claimed in (row, *co_located):
                repository.mark_delete_status(self._conn, claimed.id, "pending")
            self._conn.commit()
            return [], 0
        # A co-located row whose files are not recorded stays claimed through
        # the removal, so a keep on it is still refused, but it cannot close.
        closing = [row, *(other for other in co_located if tracked_paths(other) is not None)]
        tracked = [entry for done in closing for entry in done.tracked_files or []]
        owned = [(done, file) for done in closing for file in tracked_paths(done) or []]
        protected = fetch_tracked_paths_in_folders(
            self._conn,
            {os.path.dirname(file) for _, file in owned},
            [done.media_item_id for done in closing],
        )
        limits: dict[str, str] = {}
        for done, file in owned:
            folder, limit = str(Path(file).parent), _folder_limit(done, file)
            # Files sharing a folder walk only as far as the lowest limit allows.
            if folder not in limits or len(Path(limit).parts) > len(Path(limits[folder]).parts):
                limits[folder] = limit
        already_removed = {path for done in closing for path in done.removed_paths}
        result = _delete_files_on_disk(
            self._conn, row, tracked, allowed_roots, protected, already_removed
        )
        if result is None:
            for other in co_located:
                repository.mark_delete_status(self._conn, other.id, "pending")
            self._conn.commit()
            return [], 0
        # Recovery books a stuck row only from this record, so it is committed
        # before anything else can fail or the process can die.
        unrecorded = self._commit_record(row, closing, result)
        if result.removed:
            _remove_bare_folders(self._conn, row, result.removed, allowed_roots, limits)
        closed = self._settle(row, closing, co_located, result, unrecorded)
        # Commit *before* the Radarr/Sonarr unmonitor HTTP calls: the unmonitor
        # is best-effort and a slow response must not hold the write lock.
        self._conn.commit()
        if closed:
            self._unmonitor()
        return closed, result.bytes_freed

    def _commit_record(
        self, row: DeletionRow, closing: list[DeletionRow], result: TrackedFilesResult
    ) -> set[int]:
        """Write and commit the record of *result*, trying again on a database error.

        Returns the ids of the rows a clear made during the removal left without
        one. Not rolled back in between: once the files are gone nothing but this
        record says so.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                unrecorded = self._record_removal(row, closing, result)
                self._conn.commit()
                return unrecorded
            except sqlite3.OperationalError:
                if attempt == _RECORD_ATTEMPTS:
                    raise
                logger.warning(
                    "engine.delete.record_failed id=%s attempt=%d — trying again",
                    row.id,
                    attempt,
                    exc_info=True,
                )

    def _record_removal(
        self, row: DeletionRow, closing: list[DeletionRow], result: TrackedFilesResult
    ) -> set[int]:
        """Add what *result* removed to each closing row's record of removed files.

        The bytes freed are credited to *row* alone, so the audit total matches
        what left the disk. Returns the ids of the rows that are gone.
        """
        unrecorded: set[int] = set()
        for done in closing:
            gone = _own_removed(done, result)
            freed = result.bytes_freed if done is row else 0
            if (gone or freed) and not record_removed_files(
                self._conn,
                done.id,
                done.removed_paths | gone,
                done.removed_bytes + freed,
                {**done.removed_sizes, **_tracked_sizes(done)},
                {str(Path(path)) for path in tracked_paths(done) or []} - gone - done.removed_paths,
            ):
                unrecorded.add(done.id)
        return unrecorded

    def _settle(
        self,
        row: DeletionRow,
        closing: list[DeletionRow],
        co_located: list[DeletionRow],
        result: TrackedFilesResult,
        unrecorded: set[int],
    ) -> list[DeletionRow]:
        """Book each closing row whose files are all gone; return the rest to pending.

        A row with a tracked file not found, kept for another item or failed
        (or a sidecar failed beside one) stays pending; its record of the files
        removed so far lets the pass that finishes it close it. The bytes freed
        are credited to *row* alone, and reach its ``deleted`` audit entry
        once it closes. A row a clear took once its record was written was
        booked by the clear with that record; one it took before, in
        *unrecorded*, was booked with what earlier passes freed.
        """
        tracked = {str(Path(path)) for done in closing for path in tracked_paths(done) or []}
        closed: list[DeletionRow] = []
        # The lock comes before the reads, so no clear lands between them and the booking.
        if not self._conn.in_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
        for done in closing:
            cleared = fetch_deletion(self._conn, done.id) is None
            if cleared and done.id not in unrecorded:
                continue
            space = (0 if cleared else done.removed_bytes) + (
                result.bytes_freed if done is row else 0
            )
            left = _files_left(done, result, tracked)
            if left:
                logger.warning(
                    "engine.delete.incomplete id=%s media_id=%s not_found=%d protected=%d "
                    "failed=%d — leaving the row pending",
                    done.id,
                    done.media_item_id,
                    left.not_found,
                    left.protected,
                    len(left.failed),
                )
                repository.mark_delete_status(self._conn, done.id, "pending")
                _audit_files_left(self._conn, done, left)
                # No later pass will book what this one removed.
                if cleared and _own_removed(done, result):
                    book_deletion(self._conn, done, "Deleted", space or None)
            else:
                book_deletion(self._conn, done, "Deleted", space or None, unmonitor=True)
                closed.append(done)
        for other in co_located:
            if other not in closing:
                repository.mark_delete_status(self._conn, other.id, "pending")
        return closed

    def _cancel_if_kept(self, row: DeletionRow, now_iso: str) -> bool:
        """Cancel a claimed row, logging and auditing why, when a keep the scan honours covers it.

        Cancelled rather than left pending, so it cannot fire later with no fresh notice.
        """
        if not cancel_kept_deletion(self._conn, row.id, now_iso):
            return False
        if row.removed_paths:
            book_deletion(self._conn, row, "Deleted", row.removed_bytes or None)
        if is_fully_removed(row):
            logger.warning(
                "engine.delete.kept_too_late id=%s media_id=%s — kept after its files were "
                "removed; booking their removal",
                row.id,
                row.media_item_id,
            )
            return True
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
        unavailable: str | None,
    ) -> bool:
        """Return False, logging why, when a keep, clear or unmount since the claim stops the removal.

        The row must still be a claimed ``scheduled_deletion`` of a selected library, no keep the scan
        honours may cover it or a co-located row, and _may_remove must still
        pass, so an item under *path* kept since the claim blocks it as it
        would have before.
        """
        if not is_claimed_deletion(self._conn, row.id):
            logger.warning(
                "engine.delete.kept_after_claim id=%s media_id=%s path=%r — kept, "
                "cleared or its library unselected since the claim; leaving the files",
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
        return self._may_remove(row, path, now_iso, unavailable=unavailable)

    def _may_remove(
        self, row: DeletionRow, path: str, now_iso: str, *, unavailable: str | None
    ) -> bool:
        """Return False, logging why, when removing *path* now could lose media.

        Before the claim it runs inside the claim's transaction, so the other
        items it reads cannot change before the claim commits; it runs again
        just before the removal. *unavailable* names the folder found missing or
        empty when neither the target nor its delete root's content was on
        disk, else None; it is read before any transaction opens.
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
        if unavailable is not None:
            logger.error(
                "engine.delete.root_unavailable id=%s path=%r folder=%r — target is "
                "missing and its delete root is missing or empty (media mount down?); "
                "leaving the row pending",
                row.id,
                path,
                unavailable,
            )
            _audit_root_unavailable(self._conn, row, unavailable)
            self._waiting[row.id] = unavailable
            return False
        return True
