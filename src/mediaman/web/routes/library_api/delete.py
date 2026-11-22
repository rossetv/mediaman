"""Media delete pipeline (POST /api/media/{id}/delete).

The barrel mounts this router and re-exports _DELETE_LIMITER so tests can patch
it at the barrel.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    import sqlite3

from mediaman.db import get_db
from mediaman.services.arr._types import RadarrMovie, SonarrSeries
from mediaman.services.arr.base import ArrClient, ArrError
from mediaman.services.arr.match import verify_movie, verify_season
from mediaman.services.infra import SafeHTTPError
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.repository.delete_intents import (
    _complete_delete_intent,
    _fail_delete_intent,
    _is_not_found,
    _record_delete_intent,
)
from mediaman.web.repository.library_api import (
    DeletionRunning,
    Kept,
    MediaDeleteSnapshot,
    NotFound,
    finalise_delete_in_tx,
    other_item_uses_paths,
    recheck_deletable,
    snapshot_media_for_delete,
    tracked_files_of_same_title,
)
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-admin cap on delete triggers.  Each call initiates an Arr delete which
# may trigger a rename/move on disk; tighter than keep: 20 per minute /
# 300 per day per actor.
_DELETE_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=300,
)


def _kept_response() -> JSONResponse:
    return respond_err(
        "kept",
        status=409,
        message="This item is kept, so it cannot be deleted. Remove its keep first.",
    )


class _Refused(Exception):
    """A check before the Radarr/Sonarr delete failed; nothing was deleted."""

    def __init__(self, message: str, *, status: int = 409, error: str = "delete_refused") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.error = error

    def response(self) -> JSONResponse:
        return respond_err(self.error, status=self.status, message=self.message)


_ARR_ERRORS = (SafeHTTPError, requests.RequestException, ArrError, ValueError)


def _lookup[T](service: str, what: str, fetch: Callable[[], T]) -> T:
    """Run a Radarr/Sonarr read made before a delete, turning any failure into a refusal.

    A 404 here is never taken as "already gone": the item may live under
    another id, so nothing is deleted.
    """
    try:
        return fetch()
    except _ARR_ERRORS as exc:
        if _is_not_found(exc):
            raise _Refused(f"{service} has no entry for this {what}; nothing was deleted") from exc
        logger.warning("%s lookup failed before a delete: %s", service, exc)
        raise _Refused(
            f"Could not check this {what} in {service}; nothing was deleted",
            status=502,
            error="upstream_check_failed",
        ) from exc


def _recheck(conn: sqlite3.Connection, media_id: str) -> None:
    try:
        recheck_deletable(conn, media_id)
    except (Kept, NotFound) as exc:
        raise _Refused(
            "This item is kept, so it cannot be deleted. Remove its keep first.", error="kept"
        ) from exc
    except DeletionRunning as exc:
        raise _Refused(_DELETION_RUNNING_MESSAGE) from exc


_SECOND_ITEM_MESSAGE = (
    "More than one library item matches this {entry}, so mediaman cannot tell which "
    "copy {service} would delete; nothing was deleted"
)

_DELETION_RUNNING_MESSAGE = (
    "A scheduled deletion of this item is in progress; nothing was deleted. "
    "Try again once it finishes."
)


def _tracked_paths(snapshot: MediaDeleteSnapshot) -> list[str]:
    return [
        path
        for entry in snapshot.tracked_files or ()
        if isinstance(path := entry.get("path"), str) and path
    ]


def _local_folders(paths: list[str], arr_folder: object, levels: int) -> set[str]:
    """Return mediaman's own path of the Radarr/Sonarr folder *arr_folder*.

    Radarr/Sonarr may mount the storage elsewhere, so the folder is found as
    the parent (up to *levels* up) of a tracked file whose name matches.
    """
    if not isinstance(arr_folder, str):
        return set()
    name = arr_folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    found = set()
    for path in paths:
        for parent in list(PurePosixPath(path).parents)[:levels]:
            if name and parent.name == name:
                found.add(str(parent))
                break
    return found


def _upstream_failed(service: str) -> JSONResponse:
    return respond_err(
        "upstream_delete_failed",
        status=502,
        message=f"Upstream {service} delete failed — DB row preserved",
    )


def _unconfirmed(message: str) -> JSONResponse:
    return respond_err("delete_unconfirmed", status=409, message=message)


_RADARR_GONE_MESSAGE = (
    "Radarr no longer has this movie, so mediaman could not confirm its files are gone. "
    "The next library scan will update it."
)
_SONARR_GONE_MESSAGE = (
    "Sonarr no longer has this show, so mediaman could not confirm the season's files "
    "are gone. The next library scan will update it."
)
_SONARR_FILES_LEFT_MESSAGE = (
    "Sonarr still lists files of this season after the delete, so mediaman could not "
    "confirm they are gone. Nothing was booked as deleted; press Delete again."
)


def _delete_movie(
    client: ArrClient,
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
    username: str,
) -> tuple[int, str | JSONResponse]:
    """Verify the movie against Radarr, then delete it with its files and an import exclusion.

    Returns (intent_id, success message) or (intent_id, error response).

    Raises:
        _Refused: when a check failed before anything was deleted.
    """
    title = snapshot.title
    tmdb_id = snapshot.tmdb_id
    if not tmdb_id:
        raise _Refused("Plex has no TMDB id for this movie; nothing was deleted")
    radarr_id = snapshot.radarr_id
    entry: RadarrMovie | None
    if radarr_id:
        entry = _lookup("Radarr", "movie", lambda: client.get_movie_by_id(radarr_id))
    else:
        entry = _lookup("Radarr", "movie", lambda: client.get_movie_by_tmdb(tmdb_id))
    entry_id = entry.get("id") if entry else None
    if not entry or not isinstance(entry_id, int):
        raise _Refused("Radarr has no entry for this movie; nothing was deleted")
    match = verify_movie(entry, tmdb_id=tmdb_id, tracked_files=snapshot.tracked_files)
    if not match.ok:
        raise _Refused(match.reason)
    if any(
        verify_movie(entry, tmdb_id=tmdb_id, tracked_files=other).ok
        for other in tracked_files_of_same_title(conn, media_id, tmdb_id=tmdb_id)
    ):
        raise _Refused(_SECOND_ITEM_MESSAGE.format(entry="Radarr movie", service="Radarr"))
    # Radarr's deleteFiles removes the whole movie folder.
    folders = _local_folders(_tracked_paths(snapshot), entry.get("path"), 1)
    if not folders or other_item_uses_paths(conn, media_id, folders=folders):
        raise _Refused("Another library item uses files in this folder; nothing was deleted")
    _recheck(conn, media_id)

    intent_id = _record_delete_intent(conn, media_id, "radarr", str(entry_id))
    failed_detail = f"Radarr did not confirm the delete of '{title}' by {username}"
    try:
        client.delete_movie(entry_id)
        logger.info("Deleted '%s' via Radarr (id %s, with files + exclusion)", title, entry_id)
    except _ARR_ERRORS as exc:
        _fail_delete_intent(conn, intent_id, media_id, failed_detail, str(exc), actor=username)
        if _is_not_found(exc):
            logger.info("Radarr answered 404 deleting id %s for '%s'", entry_id, title)
            return intent_id, _unconfirmed(_RADARR_GONE_MESSAGE)
        logger.exception("Radarr delete failed for '%s'", title)
        return intent_id, _upstream_failed("Radarr")
    # rationale: any other error is still an unconfirmed delete; close the
    # intent so the reconcile cannot book the item deleted.
    except Exception as exc:
        _fail_delete_intent(conn, intent_id, media_id, failed_detail, str(exc), actor=username)
        raise
    return intent_id, (
        f"Deleted '{title}' through Radarr; files removed and added to the import exclusion list"
    )


def _delete_season(
    client: ArrClient,
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
    username: str,
) -> tuple[int, str | JSONResponse]:
    """Verify the season against Sonarr, delete its files and unmonitor it.

    The show stays in Sonarr: removing it would drop its future seasons and
    any file Sonarr holds outside this season. A failure after the season
    files were removed keeps the DB row: Sonarr did not confirm the whole
    delete, and each step is safe to retry.

    Raises:
        _Refused: when a check failed before anything was deleted.
    """
    title = snapshot.title
    tvdb_id = snapshot.tvdb_id
    season_num = snapshot.season_number
    if not tvdb_id:
        raise _Refused("Plex has no TVDB id for this show; nothing was deleted")
    if season_num is None:
        raise _Refused("mediaman does not know this season's number; nothing was deleted")
    sonarr_id = snapshot.sonarr_id
    series: SonarrSeries | None
    if sonarr_id:
        series = _lookup("Sonarr", "show", lambda: client.get_series_by_id(sonarr_id))
    else:
        series = _lookup("Sonarr", "show", lambda: client.get_series_by_tvdb(tvdb_id))
    sid = series.get("id") if series else None
    if not series or not isinstance(sid, int):
        raise _Refused("Sonarr has no entry for this show; nothing was deleted")
    episode_files = _lookup("Sonarr", "show", lambda: client.get_episode_files(sid))
    match = verify_season(
        series,
        episode_files,
        tvdb_id=tvdb_id,
        season_number=season_num,
        tracked_files=snapshot.tracked_files,
    )
    if not match.ok:
        raise _Refused(match.reason)
    others = tracked_files_of_same_title(conn, media_id, tvdb_id=tvdb_id, season_number=season_num)
    if any(
        verify_season(
            series, episode_files, tvdb_id=tvdb_id, season_number=season_num, tracked_files=other
        ).ok
        for other in others
    ):
        raise _Refused(_SECOND_ITEM_MESSAGE.format(entry="Sonarr season", service="Sonarr"))
    verified_ids = {
        ef["id"]
        for ef in episode_files
        if ef.get("seasonNumber") == season_num and isinstance(ef.get("id"), int)
    }
    tracked_paths = _tracked_paths(snapshot)
    if other_item_uses_paths(conn, media_id, files=tracked_paths):
        raise _Refused("Another library item uses files of this season; nothing was deleted")
    _recheck(conn, media_id)

    intent_id = _record_delete_intent(conn, media_id, "sonarr", str(sid))
    files_removed = False

    def failed_detail() -> str:
        detail = f"Sonarr did not confirm the delete of '{title}' S{season_num} by {username}"
        if files_removed:
            detail += "; its season files were removed"
        return detail

    try:
        client.delete_episode_files(sorted(verified_ids))
        if any(ef.get("id") in verified_ids for ef in client.get_episode_files(sid)):
            _fail_delete_intent(
                conn,
                intent_id,
                media_id,
                failed_detail(),
                "Sonarr still lists files of this season",
                actor=username,
            )
            return intent_id, _unconfirmed(_SONARR_FILES_LEFT_MESSAGE)
        files_removed = True
        client.unmonitor_season(sid, season_num)
        logger.info("Deleted season files for '%s' S%s via Sonarr", title, season_num)
    except _ARR_ERRORS as exc:
        _fail_delete_intent(conn, intent_id, media_id, failed_detail(), str(exc), actor=username)
        if _is_not_found(exc):
            logger.info("Sonarr answered 404 deleting series %s for '%s'", sid, title)
            return intent_id, _unconfirmed(_SONARR_GONE_MESSAGE)
        logger.exception("Sonarr delete failed for '%s'", title)
        return intent_id, _upstream_failed("Sonarr")
    # rationale: any other error is still an unconfirmed delete; close the
    # intent so the reconcile cannot book the item deleted.
    except Exception as exc:
        _fail_delete_intent(conn, intent_id, media_id, failed_detail(), str(exc), actor=username)
        raise
    return intent_id, f"Season {season_num} of {title} deleted through Sonarr and unmonitored"


def _run_arr_delete_branch(
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
    secret_key: str,
    username: str,
) -> tuple[int, str | JSONResponse]:
    """Delete the item through Radarr (movie) or Sonarr (season).

    Returns (intent_id, success message) or (intent_id, error response).

    Raises:
        _Refused: when a check failed before anything was deleted.
    """
    # Late import: the barrel re-exports build_radarr_from_db /
    # build_sonarr_from_db and tests patch those names at the barrel module
    # path. A top-level ``from ...library_api import …`` here would create a
    # circular import (barrel → delete → barrel), so we look them up at call
    # time via the fully-loaded barrel.
    from mediaman.web.routes.library_api import build_radarr_from_db, build_sonarr_from_db

    if snapshot.media_type == "movie":
        client = build_radarr_from_db(conn, secret_key)
        if client is None:
            raise _Refused("Radarr is not set up, so mediaman cannot delete this movie")
        return _delete_movie(client, snapshot, conn=conn, media_id=media_id, username=username)
    sonarr_client = build_sonarr_from_db(conn, secret_key)
    if sonarr_client is None:
        raise _Refused("Sonarr is not set up, so mediaman cannot delete this season")
    return _delete_season(sonarr_client, snapshot, conn=conn, media_id=media_id, username=username)


def _finalise_delete(
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
    intent_id: int,
    username: str,
) -> None:
    """Cleanup transaction: audit, deleted-media identity, scheduled_actions prune and row
    drop, wrapped in BEGIN IMMEDIATE so a SQLite failure rolls everything back (no
    half-deleted row). The pending delete-intent is marked complete after the commit.
    """
    title = snapshot.title
    rk = snapshot.plex_rating_key or ""
    detail = f"Deleted '{title}' by {username}"
    if rk:
        detail += f" [rk:{rk}]"
    finalise_delete_in_tx(conn, snapshot, media_id=media_id, audit_detail=detail, actor=username)
    _complete_delete_intent(conn, intent_id)
    logger.info("Deleted %s (%s) by %s", media_id, title, username)


@router.post("/api/media/{media_id}/delete")
def api_media_delete(
    media_id: str,
    request: Request,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Delete a media item via Radarr/Sonarr.

    Two-phase, three-transaction layout, deliberately split:

    1. Snapshot transaction: read the media row and capture identifiers, then release the
      lock.
    2. External Arr calls with no DB transaction held, since a seconds-long round-trip would
      block every other SQLite writer. The live Radarr/Sonarr entry is checked strictly
      against the files Plex reports, the keep is checked again, and only then is the
      delete sent. Any failed check answers 409 with nothing written. A delete-intent row
      is persisted before the delete so a crash between it and the cleanup can be
      reconciled.
    3. Cleanup transaction: audit row and deleted-media identity, prune scheduled_actions,
      drop the media_items row, mark the intent complete.
    """
    if not _DELETE_LIMITER.check(username):
        logger.warning("media.delete_throttled user=%s", username)
        return respond_err(
            "too_many_requests", status=429, message="Too many delete operations — slow down"
        )
    conn = get_db()

    # Snapshot transaction: the helper opens ``BEGIN IMMEDIATE`` and rolls
    # back on NotFound. We map "absent media row" to 403 so the
    # endpoint cannot be used as an existence oracle (an authenticated
    # caller learns "forbidden" rather than "not found" for unknown ids).
    try:
        snapshot = snapshot_media_for_delete(conn, media_id)
    except NotFound:
        return respond_err("forbidden", status=403)
    except Kept:
        return _kept_response()
    except DeletionRunning:
        return respond_err("delete_refused", status=409, message=_DELETION_RUNNING_MESSAGE)

    try:
        intent_id, outcome = _run_arr_delete_branch(
            snapshot,
            conn=conn,
            media_id=media_id,
            secret_key=request.app.state.config.secret_key,
            username=username,
        )
    except _Refused as refusal:
        logger.info("media.delete_refused id=%s reason=%s", media_id, refusal.message)
        return refusal.response()
    if isinstance(outcome, JSONResponse):
        return outcome

    _finalise_delete(snapshot, conn=conn, media_id=media_id, intent_id=intent_id, username=username)
    return respond_ok({"id": media_id, "message": outcome})
