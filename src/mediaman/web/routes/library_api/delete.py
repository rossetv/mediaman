"""Media delete pipeline: ``POST /api/media/{id}/delete``.

The delete flow is split out of the package barrel to keep that module
under the size ceiling. It owns the two-phase, three-transaction delete
contract, the Radarr/Sonarr delete branch helpers, the delete-intent
durability wiring, and the ``_DELETE_LIMITER`` singleton. The barrel
mounts this module's ``router`` and re-exports ``_DELETE_LIMITER`` so the
historic test patch / import targets keep working.
"""

from __future__ import annotations

import logging
import sqlite3

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.db import get_db
from mediaman.services.arr.base import ArrClient, ArrError
from mediaman.services.infra import SafeHTTPError
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.repository.delete_intents import (
    _complete_delete_intent,
    _fail_delete_intent,
    _record_delete_intent,
)
from mediaman.web.repository.library_api import (
    MediaDeleteSnapshot,
    NotFound,
    finalise_delete_in_tx,
    snapshot_media_for_delete,
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


def _is_already_gone(exc: Exception) -> bool:
    """Return True when an Arr exception carries a 404 — already-deleted upstream."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else None
    return status == 404


def _try_radarr_delete(
    client: ArrClient,
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
) -> tuple[int | None, JSONResponse | None]:
    """Delete a movie via Radarr; returns ``(intent_id, short_circuit_resp)``.

    The response is ``None`` when the caller should proceed to the cleanup
    transaction; a non-None ``JSONResponse`` means the handler must return
    immediately (e.g. 502 on an upstream failure).
    """
    title = snapshot.title
    radarr_id = snapshot.radarr_id
    if not radarr_id:
        logger.info("No stored radarr_id for '%s' — skipping Radarr-level delete.", title)
        return None, None
    intent_id = _record_delete_intent(conn, media_id, "radarr", str(radarr_id))
    try:
        client.delete_movie(radarr_id)
        logger.info("Deleted '%s' via Radarr (id %s, with files + exclusion)", title, radarr_id)
    except (SafeHTTPError, requests.RequestException, ArrError, ValueError) as exc:
        if _is_already_gone(exc):
            logger.info(
                "Radarr reports id %s already gone for '%s' — idempotent delete",
                radarr_id,
                title,
            )
            return intent_id, None
        _fail_delete_intent(conn, intent_id, str(exc))
        logger.exception("Radarr delete failed for '%s'", title)
        return intent_id, respond_err(
            "upstream_delete_failed",
            status=502,
            message="Upstream Radarr delete failed — DB row preserved",
        )
    return intent_id, None


def _try_sonarr_delete(
    sonarr_client: ArrClient,
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
) -> tuple[int | None, JSONResponse | None]:
    """Delete a TV season (and the show if empty) via Sonarr.

    Returns ``(intent_id, short_circuit_resp)``: when the response is
    ``None`` the caller proceeds to the cleanup transaction; otherwise
    the handler returns the response immediately.
    """
    title = snapshot.title
    sid = snapshot.sonarr_id
    season_num = snapshot.season_number
    if not (sid and season_num is not None):
        return None, None
    intent_id = _record_delete_intent(conn, media_id, "sonarr", str(sid))
    try:
        sonarr_client.delete_episode_files(sid, season_num)
        sonarr_client.unmonitor_season(sid, season_num)
        logger.info("Deleted season files for '%s' S%s via Sonarr", title, season_num)
        if not sonarr_client.has_remaining_files(sid):
            sonarr_client.delete_series(sid)
            logger.info(
                "No files remain for '%s' — deleted series from Sonarr with exclusion",
                title,
            )
    except (SafeHTTPError, requests.RequestException, ArrError, ValueError) as exc:
        if _is_already_gone(exc):
            logger.info(
                "Sonarr reports id %s already gone for '%s' — idempotent delete",
                sid,
                title,
            )
            return intent_id, None
        _fail_delete_intent(conn, intent_id, str(exc))
        logger.exception("Sonarr delete failed for '%s'", title)
        return intent_id, respond_err(
            "upstream_delete_failed",
            status=502,
            message="Upstream Sonarr delete failed — DB row preserved",
        )
    return intent_id, None


def _run_arr_delete_branch(
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
    secret_key: str,
) -> tuple[int | None, JSONResponse | None]:
    """Dispatch to the Radarr or Sonarr branch based on the snapshot's media type."""
    # Late import: the barrel re-exports build_radarr_from_db /
    # build_sonarr_from_db and tests patch those names at the barrel module
    # path. A top-level ``from ...library_api import …`` here would create a
    # circular import (barrel → delete → barrel), so we look them up at call
    # time via the fully-loaded barrel.
    from mediaman.web.routes.library_api import build_radarr_from_db, build_sonarr_from_db

    if snapshot.media_type == "movie":
        client = build_radarr_from_db(conn, secret_key)
        if client is None:
            return None, None
        return _try_radarr_delete(client, snapshot, conn=conn, media_id=media_id)
    sonarr_client = build_sonarr_from_db(conn, secret_key)
    if sonarr_client is None:
        return None, None
    return _try_sonarr_delete(sonarr_client, snapshot, conn=conn, media_id=media_id)


def _finalise_delete(
    snapshot: MediaDeleteSnapshot,
    *,
    conn: sqlite3.Connection,
    media_id: str,
    intent_id: int | None,
    username: str,
) -> None:
    """Cleanup transaction: audit + scheduled_actions prune + row drop.

    Wrapped in ``BEGIN IMMEDIATE`` by :func:`finalise_delete_in_tx` so a
    SQLite failure rolls the whole lot back together (fail-closed: no
    half-deleted row). The pending delete-intent row, if any, is marked
    complete after the cleanup commits.
    """
    title = snapshot.title
    rk = snapshot.plex_rating_key or ""
    detail = f"Deleted '{title}' by {username}"
    if rk:
        detail += f" [rk:{rk}]"
    finalise_delete_in_tx(
        conn,
        media_id=media_id,
        audit_detail=detail,
        space_bytes=snapshot.file_size_bytes,
        actor=username,
    )
    if intent_id is not None:
        _complete_delete_intent(conn, intent_id)
    logger.info("Deleted %s (%s) — %s by %s", media_id, title, snapshot.file_path, username)


@router.post("/api/media/{media_id}/delete")
def api_media_delete(
    media_id: str,
    request: Request,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Delete a media item via Radarr/Sonarr.

    Two-phase, three-transaction layout — intentionally split:

    1. **Snapshot transaction** (``BEGIN IMMEDIATE`` … ``COMMIT``)
       — read the media row, capture identifiers, release the lock.
    2. **External Arr call** (no DB transaction held)
       — Radarr / Sonarr round-trip can take seconds; holding a SQLite
       write lock that long would block every other writer in the process.
       A delete-intent row is persisted *before* this step so a crash
       between the Arr call returning and the DB cleanup landing can be
       reconciled by :func:`reconcile_pending_delete_intents` at startup.
    3. **Cleanup transaction** (``BEGIN IMMEDIATE`` … ``COMMIT``)
       — write the audit row, prune ``scheduled_actions``, drop the
       ``media_items`` row, then mark the delete-intent complete.
    """
    if not _DELETE_LIMITER.check(username):
        logger.warning("media.delete_throttled user=%s", username)
        return respond_err(
            "too_many_requests", status=429, message="Too many delete operations — slow down"
        )
    conn = get_db()

    # Snapshot transaction: the helper opens ``BEGIN IMMEDIATE`` and rolls
    # back on :exc:`NotFound`. We map "absent media row" to 403 so the
    # endpoint cannot be used as an existence oracle (an authenticated
    # caller learns "forbidden" rather than "not found" for unknown ids).
    try:
        snapshot = snapshot_media_for_delete(conn, media_id)
    except NotFound:
        return respond_err("forbidden", status=403)

    intent_id, short_circuit = _run_arr_delete_branch(
        snapshot,
        conn=conn,
        media_id=media_id,
        secret_key=request.app.state.config.secret_key,
    )
    if short_circuit is not None:
        return short_circuit

    _finalise_delete(snapshot, conn=conn, media_id=media_id, intent_id=intent_id, username=username)
    return respond_ok({"id": media_id})
