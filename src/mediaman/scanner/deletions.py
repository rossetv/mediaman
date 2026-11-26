"""Deletion executor — removes files and reconciles the DB.

All deletion-time concerns live here:

* Stuck-state recovery (recover_stuck_deletions) for rows left in
  the ``deleting`` state by a previous crash between the on-disk rm and
  the DB cleanup commit.
* Allowlist enforcement via repository.read_delete_allowed_roots_setting.
* The two-phase delete loop that unmonitors *arr clients only after the
  SQLite write lock has been released, and only an entry that matches.
* The record of which delete roots or folders the last pass found
  missing or empty, for the storage panel.

Imports repository; MUST NOT import fetch or
engine (see engine.py header).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

import requests

from mediaman.core.audit import log_audit
from mediaman.core.delete_root_state import read_delete_root_state, write_delete_root_state
from mediaman.core.time import now_utc
from mediaman.scanner import repository
from mediaman.scanner.repository import DeletionRow
from mediaman.scanner.repository.rekeyed import (
    Ids,
    cancel_kept_deletion,
    fetch_title,
    files_held_for_gone_titles,
    ids_match,
    is_kept_deletion,
    is_watched_lately,
    title_files,
    title_ids,
    was_held_since_scheduled,
)
from mediaman.scanner.repository.scheduled_actions import (
    DELETION_ACTION,
    audit_lost_removal,
    await_rescan,
    book_deletion,
    book_removed_files,
    claim_due_deletion,
    claim_due_deletions_under,
    clear_removal_intent,
    fetch_deletion,
    fetch_due_deletions_under,
    fetch_media_held_outside_pass,
    fetch_old_marks,
    fetch_pending_rescans,
    fetch_pending_unmonitors,
    fetch_tracked_paths_in_folders,
    finish_unmonitor,
    is_claimed_deletion,
    is_fully_removed,
    is_latest_audit,
    mark_unanswered,
    record_removal_intent,
    record_removed_files,
    tracked_paths,
    was_audited_since,
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


def _stamp(path: str) -> list[int] | None:
    """Return what tells the file at *path* from any put there after it, or None when there is none.

    A new file may reuse the inode, the size and a set modification time; its
    change time cannot be set. The last number tells its folder.
    """
    try:
        found = os.lstat(path)
        folder = os.stat(os.path.dirname(path))
    except (FileNotFoundError, NotADirectoryError):
        return None
    return [found.st_ino, found.st_ctime_ns, folder.st_ino]


def _saw_it_go(path: str, stamp: list[int]) -> bool:
    """Return True when the folder *path* was in is there and no longer holds its file.

    A folder moved away, or the bare mount point of one unmounted, tells nothing; nor
    does one that holds the file under another name.
    """
    inode, _, folder_inode = stamp
    folder = os.path.dirname(path)
    try:
        with os.scandir(folder) as entries:
            there = [entry.inode() for entry in entries]
        return inode not in there and (bool(there) or os.stat(folder).st_ino == folder_inode)
    except OSError:
        return False


def _stamps(rows: list[DeletionRow]) -> dict[str, list[int] | None]:
    """Return the stamp of each tracked file of *rows*; one that cannot be read counts as not there."""
    stamps: dict[str, list[int] | None] = {}
    for row in rows:
        for path in tracked_paths(row) or []:
            try:
                stamps[str(Path(path))] = _stamp(path)
            except (OSError, ValueError):
                stamps[str(Path(path))] = None
    return stamps


def _settle_intent(
    conn: sqlite3.Connection, row: DeletionRow, allowed_roots: list[str]
) -> DeletionRow | None:
    """Return *row* with the files its cut-off removal took recorded as removed.

    Those are the files it was about to remove that are gone from their
    folder. Their bytes are not credited: nobody counted them. While the disk
    cannot say what became of one, nothing is recorded, so the row still has
    its intent and is held until a scan says where its files are.

    Any other file at such a path, or a tracked one that was not there, may be a
    new copy: nothing proves it is what was due. The deletion is then cancelled,
    with only the files gone booked, so the item waits for a scan to schedule it
    afresh, and None returned.
    """
    if not row.removal_intent:
        return row
    expected = {path: stamp for path, stamp in row.removal_intent.items() if stamp}
    try:
        now = {path: _stamp(path) for path in expected}
    except OSError:
        return row
    absent = {path for path, stamp in now.items() if stamp is None}
    taken = {
        path
        for path in absent
        if path_within_delete_roots(path, allowed_roots) and _saw_it_go(path, expected[path])
    }
    changed = any(stamp not in (None, expected[path]) for path, stamp in now.items()) or (
        taken != absent
        and any(
            str(Path(path)) not in expected and os.path.lexists(path)
            for path in tracked_paths(row) or []
        )
    )
    if not changed and taken != absent:
        return row
    if taken:
        removed = row.removed_paths | taken
        left = (row.left_paths or frozenset()).union(row.removal_intent) - removed
        sizes = {**row.removed_sizes, **_tracked_sizes(row)}
        record_removed_files(conn, row.id, removed, row.removed_bytes, sizes, left)
        row = replace(row, removed_paths=removed, removed_sizes=sizes, left_paths=left)
    else:
        clear_removal_intent(conn, row.id)
    row = replace(row, removal_intent={})
    if not changed or row.action != DELETION_ACTION:
        return row
    logger.warning(
        "engine.delete.files_changed id=%s media_id=%s path=%r — its removal was cut off and "
        "its files are not those that were due; cancelling its deletion",
        row.id,
        row.media_item_id,
        row.file_path,
    )
    if row.removed_paths:
        book_deletion(conn, row, "Deleted (recovered)", row.removed_bytes or None)
    repository.delete_scheduled_action(conn, row.id)
    log_audit(
        conn,
        row.media_item_id,
        "delete_refused",
        f"Not deleted: {row.title} — its removal was cut off and its files changed since, so "
        "its scheduled deletion is cancelled",
    )
    return None


def recover_stuck_deletions(conn: sqlite3.Connection, *, book: bool = True) -> list[DeletionRow]:
    """Reconcile ``scheduled_actions`` rows left in the ``deleting`` state.

    A row whose removal has no recorded outcome is reconciled too: the files it
    was about to remove that are gone from their folder count as removed by it,
    and one whose files are not those that were due is cancelled (_settle_intent).

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
    * Every tracked file absent and recorded as removed -> the removal completed
      but the follow-up bookkeeping was never committed. Convert to a normal
      ``deleted`` cleanup: write the audit entry (crediting the recorded
      bytes) and drop the row. Folders left holding only artwork do not
      count as files. If a keep or snooze was written over the row during
      the removal, the keep stands: reset
      to ``pending``; the live pass then books what was removed.
    * Files absent **but** one not recorded as removed was NOT within the
      configured delete roots (an operator changed ``MEDIAMAN_DELETE_ROOTS`` between
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
        settled = _settle_intent(conn, row, allowed_roots)
        # Before the disk is read again, so a hung mount cannot hold the write lock.
        conn.commit()
        if settled is None:
            continue
        row = settled
        paths = tracked_paths(row)
        # A file this deletion removed is expected to be gone, whatever became of its
        # root since, and the folder walk after it may have emptied the folder above.
        unrecorded = [p for p in paths or () if str(Path(p)) not in row.removed_paths]
        if row.removal_intent and (paths is None or unrecorded):
            # Still not settled: the pass says why it waits.
            repository.mark_delete_status(conn, row.id, "pending")
        elif paths is None:
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
        elif not all(path_within_delete_roots(path, allowed_roots) for path in unrecorded):
            # Files are gone but an unrecorded path was never inside an allowed root —
            # mediaman did not (and could not) delete it. Do NOT write a
            # phantom "deleted" audit row. Reset to pending and shout.
            logger.error(
                "engine.delete.recover id=%s path=%r — files absent but a tracked path "
                "is outside the configured MEDIAMAN_DELETE_ROOTS (or roots are "
                "unconfigured). Refusing to fabricate a 'deleted' audit entry; "
                "reverting status to 'pending' for operator review.",
                row.id,
                row.file_path,
            )
            repository.mark_delete_status(conn, row.id, "pending")
        elif (unavailable := _first_unavailable(unrecorded, allowed_roots)) is not None:
            logger.error(
                "engine.delete.recover id=%s path=%r — files absent but their delete "
                "root is missing or empty (media mount down?); reverting status "
                "to 'pending' instead of booking it as deleted.",
                row.id,
                row.file_path,
            )
            _audit_root_unavailable(conn, row, unavailable)
            repository.mark_delete_status(conn, row.id, "pending")
        elif unrecorded:
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
            audit_lost_removal(conn, row, booked=True)
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
    closing: list[DeletionRow],
) -> TrackedFilesResult | None:
    """Remove *tracked_files*, those of the *closing* rows, with their sidecars.

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
        for done in closing:
            clear_removal_intent(conn, done.id)
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


def _gave_no_answer(exc: Exception) -> bool:
    """Return True when *exc* says the service did not deal with the request, rather than refused it."""
    if isinstance(exc, SafeHTTPError):
        # 0 is a transport error; a proxy answers 502-504 for a service that is down.
        return exc.status_code in (0, 401, 403, 502, 503, 504)
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _is_down(exc: Exception) -> bool:
    """Return True when *exc* says the service would answer no other request now either."""
    if isinstance(exc, SafeHTTPError):
        return exc.status_code in (401, 403) or isinstance(exc.__cause__, requests.ConnectionError)
    return isinstance(exc, requests.ConnectionError)


# The time one pass may spend waiting for answers a service does not give.
_MAX_WAIT_SECONDS = 120.0
# A mark left longer is dropped: the title may have been added back in Radarr/Sonarr since.
_MARK_MAX_AGE = timedelta(days=7)


@dataclass
class ArrTurn:
    """What one pass has found out about Radarr and Sonarr.

    *unusable* names those not to ask anything more; *unanswered* holds the
    marks, by audit id, not to ask about again.
    """

    unusable: set[str] = field(default_factory=set)
    unanswered: set[int] = field(default_factory=set)
    waited: dict[str, float] = field(default_factory=dict)

    def note_no_answer(self, service: str, exc: Exception, began: float) -> None:
        self.waited[service] = self.waited.get(service, 0.0) + monotonic() - began
        if _is_down(exc) or self.waited[service] >= _MAX_WAIT_SECONDS:
            self.unusable.add(service)


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


def _unmonitor_movie(row: DeletionRow, client: ArrClient) -> int | str:
    """Unmonitor the Radarr entry matching *row*; return its id, or why none matches."""
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
    return radarr_id


def _unmonitor_season(row: DeletionRow, client: ArrClient) -> int | str:
    """Unmonitor the Sonarr season matching *row*; return its show's id, or why none matches."""
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
    return sonarr_id


def _unmonitor(row: DeletionRow, client: ArrClient) -> int | str | None:
    """Stop Radarr/Sonarr re-downloading a deleted item.

    Returns the id of the entry unmonitored, or why none was when that must be
    told. Only ever unmonitors, after checking it is the same item: the files are
    already gone, so a wrong match could at worst stop a download, never delete
    anything. With no entry for the item, there is nothing to unmonitor. Raises
    when the service gives no answer.
    """
    is_movie = row.media_type == "movie"
    service = _service(row)
    try:
        outcome = _unmonitor_movie(row, client) if is_movie else _unmonitor_season(row, client)
    except _ARR_ERRORS as exc:
        if _gave_no_answer(exc):
            raise
        logger.warning("Unmonitor in %s failed for %r", service, row.title, exc_info=True)
        outcome = f"{service} returned an error"
    if isinstance(outcome, int):
        return outcome
    reason = outcome
    if reason == _no_entry(is_movie):
        # Nothing to unmonitor, and nothing it could download again.
        logger.info("engine.delete.unmonitor_none media_id=%s — %s", row.media_item_id, reason)
        return None
    return _not_unmonitored(row, reason)


def _service(row: DeletionRow) -> str:
    return "Radarr" if row.media_type == "movie" else "Sonarr"


def _not_unmonitored(row: DeletionRow, reason: str) -> str:
    logger.warning("engine.delete.unmonitor_skipped media_id=%s — %s", row.media_item_id, reason)
    return (
        f"Not unmonitored in {_service(row)}: {row.title} — {reason}; it was deleted, but "
        f"{_service(row)} may download it again"
    )


def _on_disk_again(row: DeletionRow) -> bool:
    paths = row.removed_paths.union(tracked_paths(row) or [])
    return any(os.path.lexists(path) for path in paths)


def _entry_to_rescan(row: DeletionRow, client: ArrClient) -> int | None:
    """Return the id of the Radarr/Sonarr entry with *row*'s TMDB/TVDB id, or None."""
    if row.media_type == "movie":
        own_id, key, stored_id = row.tmdb_id, "tmdbId", row.radarr_id
        lookups: tuple[Callable[[int], Any], Callable[[int], Any]] = (
            client.get_movie_by_id,
            client.get_movie_by_tmdb,
        )
    else:
        own_id, key, stored_id = row.tvdb_id, "tvdbId", row.sonarr_id
        lookups = client.get_series_by_id, client.get_series_by_tvdb
    if not own_id:
        return None
    entry = _lookup_entry(stored_id, *lookups, own_id)
    if not entry or entry.get(key) != own_id or not isinstance(entry.get("id"), int):
        return None
    return int(entry["id"])


class _Told(NamedTuple):
    """What became of a booked deletion's mark: the entry left to rescan, and what to audit.

    With *keep* the mark stays as it is, for the next call to try again.
    """

    rescan_id: int | None = None
    note: str | None = None
    keep: bool = False


def _tell(
    conn: sqlite3.Connection,
    row: DeletionRow,
    *,
    radarr_client: ArrClient | None,
    sonarr_client: ArrClient | None,
    turn: ArrTurn,
) -> _Told:
    """Unmonitor a booked deletion, or find why not; *turn* learns of a service giving no answer."""
    service = _service(row)
    client = radarr_client if row.media_type == "movie" else sonarr_client
    if client is None and service not in turn.unusable:
        return _Told()
    left = f"Left monitored in {service}: {row.title}"
    if _on_disk_again(row):
        return _Told(note=f"{left} — a copy of it is on disk again")
    later = _Told(
        note=f"Not unmonitored in {service} yet: {row.title} — {service} cannot be used now; "
        "it may download it again until it is told",
        keep=True,
    )
    if client is None or service in turn.unusable:
        return later
    began = monotonic()
    if was_requested_since(conn, row):
        note = f"{left} — it was asked for again after its deletion"
        try:
            return _Told(_entry_to_rescan(row, client), note)
        except _ARR_ERRORS as exc:
            logger.warning("Lookup for a rescan failed for %r", row.title, exc_info=True)
            if _gave_no_answer(exc):
                turn.note_no_answer(service, exc, began)
            turn.unanswered.add(row.id)
            return _Told(keep=True)
    try:
        outcome = _unmonitor(row, client)
    except _ARR_ERRORS as exc:
        logger.warning("%s gave no answer to unmonitor %r", service, row.title, exc_info=True)
        turn.note_no_answer(service, exc, began)
        turn.unanswered.add(row.id)
        return later
    return _Told(rescan_id=outcome) if isinstance(outcome, int) else _Told(note=outcome)


def _drop_old_marks(
    conn: sqlite3.Connection,
    *,
    radarr_client: ArrClient | None,
    sonarr_client: ArrClient | None,
    turn: ArrTurn,
) -> None:
    """Drop, auditing each, the marks older than _MARK_MAX_AGE of a service that is set up."""
    for row, rescan in fetch_old_marks(conn, (now_utc() - _MARK_MAX_AGE).isoformat()):
        service = _service(row)
        client = radarr_client if row.media_type == "movie" else sonarr_client
        if client is None and service not in turn.unusable:
            continue
        waited = f"within {_MARK_MAX_AGE.days} days of its deletion"
        note = (
            f"Not rescanned in {service}: {row.title} — {service} could not be asked to {waited}; "
            "it may still list the deleted files"
            if rescan
            else f"Left monitored in {service}: {row.title} — {service} could not be told "
            f"{waited}, so it may download it again"
        )
        logger.warning("engine.delete.mark_dropped media_id=%s — %s", row.media_item_id, note)
        with conn:
            if finish_unmonitor(conn, row.id):
                log_audit(conn, row.media_item_id, "unmonitor_skipped", note)


def unmonitor_deleted(
    conn: sqlite3.Connection,
    *,
    radarr_client: ArrClient | None,
    sonarr_client: ArrClient | None,
    turn: ArrTurn,
) -> None:
    """Unmonitor in Radarr/Sonarr every booked deletion still marked for it.

    The mark of an entry unmonitored then waits for rescan_deleted. A service
    without a client is not set up, so there is nothing to unmonitor; one *turn*
    finds unusable is set up but cannot be used now. *turn* is the pass's own,
    so a title given no answer is not waited for again.

    Runs after the deletion committed, so a slow response never holds the write
    lock, and commits each item's outcome as it goes: one a dead process left
    marked is done by the next call. It is tried once, or kept marked while its
    service gives no answer, but not for ever; a skip is audited once as
    ``unmonitor_skipped`` and the deletion stays booked either way. A title
    that came back meanwhile is left monitored; one asked for again is only
    rescanned, as Radarr/Sonarr would not fetch files it still lists.
    """
    _drop_old_marks(conn, radarr_client=radarr_client, sonarr_client=sonarr_client, turn=turn)
    for row in fetch_pending_unmonitors(conn):
        if row.id in turn.unanswered:
            continue
        try:
            told = _tell(
                conn,
                row,
                radarr_client=radarr_client,
                sonarr_client=sonarr_client,
                turn=turn,
            )
        except sqlite3.Error:
            raise
        # rationale: the mark of a row that always fails would stop every later pass and sync.
        except Exception as exc:
            logger.exception("engine.delete.unmonitor_failed media_id=%s", row.media_item_id)
            told = _Told(
                note=_not_unmonitored(
                    row, f"an unexpected error stopped it ({exc.__class__.__name__})"
                )
            )
        with conn:
            if told.keep:
                news = told.note is not None and not was_audited_since(
                    conn, row.id, row.media_item_id, "unmonitor_skipped", told.note
                )
                if row.id in turn.unanswered:
                    mark_unanswered(conn, row.id)
            elif told.rescan_id is not None:
                news = await_rescan(conn, row.id, told.rescan_id)
            else:
                news = finish_unmonitor(conn, row.id)
            if news and told.note is not None:
                log_audit(conn, row.media_item_id, "unmonitor_skipped", told.note)


def rescan_deleted(
    conn: sqlite3.Connection,
    *,
    radarr_client: ArrClient | None,
    sonarr_client: ArrClient | None,
    turn: ArrTurn,
) -> None:
    """Ask Radarr/Sonarr to look at the disk again for the entries whose files were deleted.

    Until it does it still lists the files, so a title asked for again reads as
    already there. An entry's mark goes once its rescan is asked for or the
    service refused the entry itself, so the next call asks for one a dead
    process or a failure left. A service *turn* finds unusable is asked nothing
    more. The marks of one without a client, so not set up, go.
    """
    for movies, client in ((True, radarr_client), (False, sonarr_client)):
        service = "Radarr" if movies else "Sonarr"
        try:
            for entry_id, audit_ids in fetch_pending_rescans(conn, movies=movies).items():
                if service in turn.unusable:
                    break
                asked = client is None or _ask_rescan(client, entry_id, service, turn)
                with conn:
                    for audit_id in audit_ids:
                        if asked:
                            finish_unmonitor(conn, audit_id)
                        else:
                            mark_unanswered(conn, audit_id)
        # rationale: the deletions are booked and the marks stay for the next call.
        except Exception:
            logger.warning("Rescan of deleted titles failed (%s)", service, exc_info=True)


def _ask_rescan(client: ArrClient, entry_id: int, service: str, turn: ArrTurn) -> bool:
    """Ask for a rescan of the entry; return False when it is to be asked for again later."""
    began = monotonic()
    try:
        (client.rescan_movie if service == "Radarr" else client.rescan_series)(entry_id)
    # rationale: one entry's failure, whatever it is, must not hold up the others.
    except Exception as exc:
        if isinstance(exc, SafeHTTPError) and exc.status_code in (400, 404):
            logger.info("engine.delete.rescan_refused entry_id=%s", entry_id)
            return True
        logger.warning("Rescan failed (%s entry %s)", service, entry_id, exc_info=True)
        turn.note_no_answer(service, exc, began)
        return False
    return True


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


def _audit_unsettled(conn: sqlite3.Connection, row: DeletionRow) -> None:
    _audit_once(
        conn,
        row,
        "delete_awaiting_scan",
        f"Not deleted yet: {row.title} — its removal was cut off and what it took is not known "
        "yet; the deletion waits for a scan to record where its files are",
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


# What the history says of a title whose keep or watch holds a file: the deletion waits for it.
_GONE = "Plex lists no more"
# What it says of a deletion that waits for a library, and of one whose library is unticked.
_UNLISTED_LIBRARY = "library Plex does not list now"
_UNSELECTED = "its library is not selected in Settings"
_UNREADABLE_LIMIT = "library whose disk limit in Settings cannot be read"
# A deletion that waited for one of these is decided afresh once the wait is over.
_ENDS_NOTICE = (_GONE, _UNLISTED_LIBRARY, _UNSELECTED, _UNREADABLE_LIMIT)
# What it says of a title of a library Plex does not list now.
_WAITS_FOR_LIBRARY = (
    "the deletion waits while mediaman still has that one and Plex does not list its library"
)


class _Title(NamedTuple):
    """What tells whether two items are one title; its files as normalised paths."""

    is_film: bool
    ids: Ids
    files: set[str]
    is_kept: bool
    is_watched_lately: bool

    def may_be(self, other: _Title) -> bool:
        return self.is_film == other.is_film and (
            ids_match(self.ids, other.ids) or not self.files.isdisjoint(other.files)
        )


def _title(conn: sqlite3.Connection, media_id: str, inactivity_days: int) -> _Title | None:
    title = fetch_title(conn, media_id)
    if title is None:
        return None
    return _Title(
        title["media_type"] == "movie",
        title_ids(title),
        set(map(os.path.normpath, title_files(title))),
        repository.is_protected(conn, media_id)
        or repository.is_show_kept_pure(conn, title["show_rating_key"]),
        is_watched_lately(conn, media_id, inactivity_days),
    )


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
        inactivity_days: Days without a watch before the scan schedules a title.
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
        inactivity_days: int = 30,
        sonarr_client: ArrClient | None = None,
        radarr_client: ArrClient | None = None,
    ) -> None:
        self._conn = conn
        self._dry_run = dry_run
        self._cleanup_snoozes = cleanup_snoozes
        self._inactivity_days = inactivity_days
        self._sonarr = sonarr_client
        self._radarr = radarr_client
        # Action row id -> the folder found missing or empty that holds it back.
        self._waiting: dict[int, str] = {}
        self._unread_libraries: Collection[str] = ()
        self._gone_libraries: Collection[str] = ()
        # The titles of the libraries Plex does not list now.
        self._gone_titles: list[_Title] = []
        self._unreadable_limits: Collection[str] = ()
        # The titles of those libraries.
        self._limit_titles: list[_Title] = []
        # The files held for titles removed from the library: True by a keep, False by a watch.
        self._held_files: dict[str, bool] = {}
        self._turn = ArrTurn()

    def execute(
        self,
        *,
        unread_libraries: Collection[str] = (),
        gone_libraries: Collection[int] = (),
        unreadable_limits: Collection[str] = (),
    ) -> DeletionResult:
        """Run the deletion pass; the items of *unread_libraries* (Plex ids) wait.

        So does an item of a library of *unreadable_limits*, which the scan left out, or
        that may be one of it. So does one that may be one of *gone_libraries*, those with items stored
        that Plex does not list: while their items are stored, what keeps them
        has not come to it. And one with a file that a keep or a late watch of a
        title removed from the library still holds: a title listed with that file
        may be one the keep or the watch is never given to.

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
        4. Live: record which roots or folders held rows back, for the storage panel.
        5. Clean up expired snoozes unless dry-run.
        6. Live: ask Radarr/Sonarr to rescan what was unmonitored, then or by an earlier run.
        """
        now = now_utc()
        self._unread_libraries = unread_libraries
        self._gone_libraries = {str(library) for library in gone_libraries}
        self._gone_titles = self._titles_of(gone_libraries)
        self._unreadable_limits = unreadable_limits
        self._limit_titles = self._titles_of(
            int(library) for library in unreadable_limits if library.isdecimal()
        )
        self._held_files = files_held_for_gone_titles(self._conn, self._inactivity_days)
        self._turn = ArrTurn()
        rows = self._recover_then_fetch_pending(now)
        if self._dry_run:
            self._preview(rows, now)
            return {"deleted": 0, "reclaimed_bytes": 0}
        result = self._execute_live(rows, now)
        # Last, so a slow answer delays no deletion.
        rescan_deleted(
            self._conn,
            radarr_client=self._radarr,
            sonarr_client=self._sonarr,
            turn=self._turn,
        )
        return result

    def _titles_of(self, libraries: Iterable[int]) -> list[_Title]:
        ids = repository.fetch_ids_in_libraries(self._conn, list(libraries))
        titles = (_title(self._conn, media_id, self._inactivity_days) for media_id in ids)
        return [title for title in titles if title is not None]

    def _preview(self, rows: list[DeletionRow], now: datetime) -> None:
        """Record what *would* be deleted and which folders would hold rows back.

        Mutates no deletion: only ``dry_run_skip`` rows, why a row waits past its
        notice and the record of unavailable folders, which the storage panel would
        otherwise keep showing from the last live pass however long the passes stay
        dry runs. A wait that starts and ends in dry runs must still end the notice.
        """
        allowed_roots = repository.read_delete_allowed_roots_setting(self._conn)
        held = {row.id: self._held_back(row, now.isoformat()) for row in rows}
        due = [
            row
            for row in rows
            if row.library_selected
            and not held[row.id]
            and not was_held_since_scheduled(self._conn, row.id, _ENDS_NOTICE)
            and not is_kept_deletion(self._conn, row.id, now.isoformat())
        ]
        # Read the disk before writing anything, so a hung mount cannot hold the lock.
        waiting: dict[str, list[int]] = {}
        for row in due:
            if allowed_roots and (folder := _unavailable_for(row, allowed_roots)) is not None:
                waiting.setdefault(folder, []).append(row.id)
        for row in rows:
            if not row.library_selected:
                _audit_once(
                    self._conn,
                    row,
                    "delete_awaiting_scan",
                    f"Not deleted yet: {row.title} — {_UNSELECTED}",
                )
            elif held[row.id]:
                self._say_it_waits(row, held[row.id])
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
        try:
            unmonitor_deleted(
                self._conn,
                radarr_client=self._radarr,
                sonarr_client=self._sonarr,
                turn=self._turn,
            )
        # rationale: the deletions are committed and the marks stay for the next pass, so
        # nothing here may leave the rows still due unhandled.
        except Exception:
            logger.exception("engine.delete.unmonitor_pass_failed — the next pass tries again")

    def _execute_live(self, rows: list[DeletionRow], now: datetime) -> DeletionResult:
        """Delete each eligible row from disk, fail-closed on an empty allowlist."""
        deleted_count = 0
        reclaimed_bytes = 0
        self._waiting = {}
        book_removed_files(self._conn)
        self._conn.commit()
        allowed_roots = repository.read_delete_allowed_roots_setting(self._conn)
        # Booking what was removed deletes nothing, so it does not wait for roots.
        rows = self._book_over(rows, allowed_roots, now.isoformat())
        # Fail closed once, before the loop: deletion must refuse when
        # MEDIAMAN_DELETE_ROOTS is unset. Checked outside the per-row
        # loop so the refusal is logged a single time, not once per pending
        # row. Snooze cleanup below still runs so expired snoozes
        # re-enter the pipeline once roots are configured.
        if not allowed_roots:
            # Cancelling deletes nothing, so it does not wait for roots either.
            for row in rows:
                if not row.library_selected and self._claim(row, now.isoformat()) is not None:
                    repository.mark_delete_status(self._conn, row.id, "pending")
                    self._conn.commit()
            rows = [row for row in rows if row.library_selected]
            for row in rows:
                if waiting := self._held_back(row, now.isoformat()):
                    self._say_it_waits(row, waiting)
            self._conn.commit()
            if rows:
                logger.error(
                    "Skipping %d pending deletion(s): no delete folders are configured. "
                    "Set the MEDIAMAN_DELETE_ROOTS env var (colon-separated).",
                    len(rows),
                )
        else:
            for row in rows:
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

    def _must_wait(self, row: DeletionRow) -> bool:
        """Return True while what Plex answered does not tell whether *row*'s item may go.

        Or while a file of it is held for a title removed from the library.
        """
        return (
            row.library_id in self._unread_libraries
            or self._waits_for_limit(row)
            or bool(self._gone_twins(row))
            or self._is_file_held(row)
        )

    def _held_back(self, row: DeletionRow, now_iso: str) -> list[DeletionRow]:
        """Return what holds *row* back now: itself, else the due rows in the folder it would remove.

        Asked once, before anything else is decided: nothing is removed, no row
        in the folder is claimed and no wait is told as over while it names a row.
        """
        if self._must_wait(row):
            return [row]
        path = row.file_path or ""
        if not path.strip():
            return []
        under = fetch_due_deletions_under(self._conn, row.media_item_id, path, now_iso)
        return [other for other in under if self._must_wait(other)]

    def _is_file_held(self, row: DeletionRow) -> bool:
        return self._held_by(row) is not None

    def _held_by(self, row: DeletionRow) -> bool | None:
        """Return what holds a file of *row* for a title removed from the library.

        True for a keep, False for a late watch alone, None when nothing does.
        """
        files = map(os.path.normpath, tracked_paths(row) or [])
        folders = tuple(place for place in self._held_files if place.endswith(os.sep))
        holds = [
            self._held_files[place]
            for file in (row.media_item_id, *files)
            for place in (file, *filter(file.startswith, folders))
            if place in self._held_files
        ]
        return any(holds) if holds else None

    def _gone_twins(self, row: DeletionRow) -> list[_Title]:
        """Return the titles of libraries Plex does not list that *row*'s item may be."""
        return self._twins(row, self._gone_titles)

    def _twins(self, row: DeletionRow, titles: list[_Title]) -> list[_Title]:
        if not titles:
            return []
        own = _title(self._conn, row.media_item_id, self._inactivity_days)
        return [title for title in titles if own is not None and own.may_be(title)]

    def _waits_for_limit(self, row: DeletionRow) -> bool:
        """Return True if *row* is, or may be, of a library left out for its disk limit."""
        return row.library_id in self._unreadable_limits or bool(
            self._twins(row, self._limit_titles)
        )

    def _why_it_waits(self, row: DeletionRow) -> str:
        """Say which title or library Plex does not list holds *row* back, and for how long."""
        if self._waits_for_limit(row):
            return (
                f"it is, or may be, of a {_UNREADABLE_LIMIT}; the deletion waits until that is "
                "put right"
            )
        if row.library_id in self._gone_libraries:
            return f"it is of a {_UNLISTED_LIBRARY}; the deletion waits until Plex lists that one"
        twins = self._gone_twins(row)
        if any(gone.is_kept for gone in twins):
            return f"it may be a title that is kept in a {_UNLISTED_LIBRARY}; {_WAITS_FOR_LIBRARY}"
        files = set(map(os.path.normpath, tracked_paths(row) or []))
        # Only a file of its own makes a watch hold: the watch of a copy with other files does not.
        if any(gone.is_watched_lately and not gone.files.isdisjoint(files) for gone in twins):
            return (
                f"it has a file of a title watched lately in a {_UNLISTED_LIBRARY}; "
                f"{_WAITS_FOR_LIBRARY}"
            )
        if twins:
            return f"it may be a title of a {_UNLISTED_LIBRARY}; {_WAITS_FOR_LIBRARY}"
        if self._held_by(row):
            return (
                f"one of its files is of a kept title {_GONE}; the deletion waits while that "
                "one is kept"
            )
        return (
            f"one of its files is of a title watched lately that {_GONE}; the deletion waits "
            "until that watch is too long ago"
        )

    def _book_over(
        self, rows: list[DeletionRow], allowed_roots: list[str], now_iso: str
    ) -> list[DeletionRow]:
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
            if not replaced and not self._cannot_close(row, allowed_roots, now_iso):
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
                audit_lost_removal(self._conn, row, booked=True)
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

    def _cannot_close(self, row: DeletionRow, allowed_roots: list[str], now_iso: str) -> bool:
        """Return True for a fully removed row the removal would hold back, refuse or drop."""
        path = row.file_path or ""
        files = tracked_paths(row)
        return is_fully_removed(row) and (
            not row.library_selected
            or self._must_wait(row)
            or files is None
            or not all(path_within_delete_roots(file, allowed_roots) for file in files)
            or not path.strip()
            or bool(fetch_media_held_outside_pass(self._conn, row.media_item_id, path, now_iso))
        )

    def _claim(self, row: DeletionRow, now_iso: str) -> DeletionRow | None:
        """Claim *row* and return it as it is now, its transaction left open.

        None, committed, when it is no longer due, or was cancelled: its item is
        kept, or its library is not ticked.
        """
        if not claim_due_deletion(self._conn, row.id, now_iso):
            self._conn.commit()
            logger.info(
                "engine.delete.skipped id=%s media_id=%s — no longer a due deletion",
                row.id,
                row.media_item_id,
            )
            return None
        # A row that closed with another's removal may hold a newer record.
        row = fetch_deletion(self._conn, row.id) or row
        if self._cancel_if_kept(row, now_iso):
            self._conn.commit()
            return None
        if not row.library_selected:
            self._refuse_unselected(row)
            self._conn.commit()
            return None
        return row

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

        The claim's own commit also names the files about to go, so a removal
        whose outcome is never recorded is told from a new copy put there since.

        Due rows of other items stored at or under the path are claimed in
        the same transaction and their tracked files removed with this row's;
        each closes once all its own files are gone. They go back to pending
        if the removal fails, or if a keep or clear made after the claim stops it.
        """
        # Read the disk before the claim opens a write transaction, so a hung
        # mount cannot hold the database lock.
        unavailable = _unavailable_for(row, allowed_roots)
        path = row.file_path or ""
        with_it = (
            fetch_due_deletions_under(self._conn, row.media_item_id, path, now_iso)
            if path.strip()
            else []
        )
        stamps = _stamps([row, *with_it])
        claimed_row = self._claim(row, now_iso)
        if claimed_row is None:
            return [], 0
        row = claimed_row
        path = row.file_path or ""

        waiting = self._held_back(row, now_iso)
        if waiting:
            repository.mark_delete_status(self._conn, row.id, "pending")
            self._say_it_waits(row, waiting)
            self._conn.commit()
            return [], 0

        if self._cancel_if_released(row):
            self._conn.commit()
            return [], 0

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
            audit_lost_removal(self._conn, row, booked=False)
            repository.delete_scheduled_action(self._conn, row.id)
            self._conn.commit()
            return [], 0

        if row.removal_intent:
            logger.warning(
                "engine.delete.unsettled id=%s media_id=%s — what its last removal took "
                "is not known yet; leaving the row pending",
                row.id,
                row.media_item_id,
            )
            repository.mark_delete_status(self._conn, row.id, "pending")
            if unavailable is not None:
                _audit_root_unavailable(self._conn, row, unavailable)
                self._waiting[row.id] = unavailable
            else:
                _audit_unsettled(self._conn, row)
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
        # An item cancelled here, kept or with a wait that is over, no longer counts
        # as due, so the check before the removal finds its media held and leaves the path.
        co_located = [
            other
            for other in claim_due_deletions_under(self._conn, row.media_item_id, path, now_iso)
            if not self._cancel_if_kept(other, now_iso) and not self._cancel_if_released(other)
        ]
        # A co-located row whose files are not recorded stays claimed through
        # the removal, so a keep on it is still refused, but it cannot close.
        closing = [row, *(other for other in co_located if tracked_paths(other) is not None)]
        # Their own files must not go now, nor their record be written over.
        waiting = [other for other in co_located if other.removal_intent or self._must_wait(other)]
        for done in closing if not waiting else []:
            intent = {
                str(Path(file)): stamps.get(str(Path(file))) for file in tracked_paths(done) or []
            }
            if any(intent.values()):
                record_removal_intent(self._conn, done, intent)
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
        # A file put there since the claim read the disk is not in its intent.
        changed = [file for file, stamp in _stamps(closing).items() if stamp != stamps.get(file)]
        if waiting:
            self._hold_with(row, waiting)
        elif changed and not replaced:
            logger.warning(
                "engine.delete.files_changed_after_claim id=%s path=%r files=%r — put there or "
                "changed since the claim; leaving the files",
                row.id,
                path,
                changed,
            )
            _audit_once(
                self._conn,
                row,
                "delete_incomplete",
                f"Not deleted yet: {row.title} — {changed[0]} was put there or changed as it was "
                "about to be removed; the next pass tries again",
            )
        # A keep or clear may have landed while the disk was read; read the
        # rows after the disk, so the window left before the removal is small.
        if (
            waiting
            or replaced
            or changed
            or not self._still_removable(row, co_located, path, now_iso, unavailable=unavailable)
        ):
            for claimed in (row, *co_located):
                if not waiting:
                    clear_removal_intent(self._conn, claimed.id)
                repository.mark_delete_status(self._conn, claimed.id, "pending")
            self._conn.commit()
            return [], 0
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
            self._conn, row, tracked, allowed_roots, protected, already_removed, closing
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
            if not gone and not freed:
                clear_removal_intent(self._conn, done.id)
            elif not record_removed_files(
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

    def _say_it_waits(self, row: DeletionRow, waiting: list[DeletionRow]) -> None:
        """Log and audit that *row* waits for itself or for the rows *waiting* under its path.

        A failure to read the library of *row* alone is told with the scan.
        """
        if waiting != [row]:
            self._hold_with(row, waiting)
            return
        logger.info(
            "engine.delete.library_unread id=%s media_id=%s — its library, or one it "
            "may have moved from, was not read from Plex; leaving the row pending",
            row.id,
            row.media_item_id,
        )
        if (
            row.library_id not in self._unread_libraries
            or self._waits_for_limit(row)
            or self._waits_for_library(row)
        ):
            _audit_once(
                self._conn,
                row,
                "delete_awaiting_scan",
                f"Not deleted yet: {row.title} — {self._why_it_waits(row)}",
            )

    def _waits_for_library(self, row: DeletionRow) -> bool:
        """Return True while Plex does not list the library of *row*, or of a title it may be."""
        return row.library_id in self._gone_libraries or bool(self._gone_twins(row))

    def _hold_with(self, row: DeletionRow, waiting: list[DeletionRow]) -> None:
        """Log and audit that *row* waits, as the rows *waiting* under its path must."""
        logger.warning(
            "engine.delete.held_with id=%s media_id=%s path=%r waiting_rows=%s — leaving the files",
            row.id,
            row.media_item_id,
            row.file_path,
            [other.id for other in waiting],
        )
        names = ", ".join(str(other.title) for other in waiting)
        # Said so that this row too is scheduled afresh once the wait is over.
        whom = ""
        if any(map(self._is_file_held, waiting)):
            whom = f" for a title {_GONE}"
        elif any(map(self._waits_for_limit, waiting)):
            whom = f" for a {_UNREADABLE_LIMIT}"
        elif any(map(self._waits_for_library, waiting)):
            whom = f" for a {_UNLISTED_LIBRARY}"
        _audit_once(
            self._conn,
            row,
            "delete_awaiting_scan",
            f"Not deleted yet: {row.title} — its folder also holds media whose own deletion "
            f"waits{whom} ({names})",
        )

    def _refuse_unselected(self, row: DeletionRow) -> None:
        """Cancel a claimed row whose library is not ticked, booking what it removed.

        Cancelled rather than left pending, so ticking the library again cannot
        fire it with no fresh scan, grace and notice.
        """
        logger.info(
            "engine.delete.library_unselected id=%s media_id=%s — its library is not selected",
            row.id,
            row.media_item_id,
        )
        self._cancel(row, f"{_UNSELECTED}, so its scheduled deletion is cancelled")

    def _cancel_if_released(self, row: DeletionRow) -> bool:
        """Cancel a claimed row that waited for what is gone from Plex or unticked in Settings.

        Run it only once _held_back found nothing holding the row or its folder.
        Its notice named a day long past, and it may have been watched since:
        the next scan decides afresh, with a new notice.
        """
        if not was_held_since_scheduled(self._conn, row.id, _ENDS_NOTICE):
            return False
        logger.info(
            "engine.delete.released id=%s media_id=%s — what it waited for is over; cancelling "
            "the deletion, whose notice is past",
            row.id,
            row.media_item_id,
        )
        self._cancel(
            row,
            "what it waited for is over, but the day its notice named is past, so its scheduled "
            "deletion is cancelled and the next scan decides afresh",
        )
        return True

    def _cancel(self, row: DeletionRow, why: str) -> None:
        """Cancel a claimed row, booking what it removed and auditing *why* it is not deleted."""
        audit_lost_removal(self._conn, row)
        finished = is_fully_removed(row)
        if row.removed_paths:
            book_deletion(self._conn, row, "Deleted", row.removed_bytes or None, unmonitor=finished)
        if finished:
            return
        repository.delete_scheduled_action(self._conn, row.id)
        log_audit(
            self._conn, row.media_item_id, "delete_refused", f"Not deleted: {row.title} — {why}"
        )

    def _cancel_if_kept(self, row: DeletionRow, now_iso: str) -> bool:
        """Cancel a claimed row, logging and auditing why, when a keep the scan honours covers it.

        Cancelled rather than left pending, so it cannot fire later with no fresh notice.
        """
        if not cancel_kept_deletion(self._conn, row.id, now_iso):
            return False
        audit_lost_removal(self._conn, row)
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
