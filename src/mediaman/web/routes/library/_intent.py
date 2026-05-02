"""Delete-intent state machine and the ``/api/media/{id}/delete`` handler.

Split out of :mod:`.api` to keep that module focused on orchestration.
The intent machinery (finding 24 — recoverable manual deletes) and the
reconcile-on-startup helper live here together because they form a
self-contained recovery subsystem.

The route handler keeps its dependencies on Radarr/Sonarr client builders
*via the parent ``api`` module's namespace* so that existing tests can
patch ``mediaman.web.routes.library.api.build_radarr_from_db`` and have
the patch take effect — the lookup happens at call time, not import
time.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.audit import log_audit
from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import ActionRateLimiter
from mediaman.db import get_db

logger = logging.getLogger("mediaman")

router = APIRouter()


# Per-admin cap on media deletes.
_DELETE_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=300,
)


def _record_delete_intent(
    conn: sqlite3.Connection,
    media_item_id: str,
    target_kind: str,
    target_id: str,
) -> int:
    """Insert a delete intent row and return its ``id``.

    Must be called *before* the external Radarr/Sonarr delete so that a
    crash between the external call and the local DB cleanup can be detected
    and reconciled on startup via :func:`reconcile_pending_delete_intents`.
    """
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO delete_intents "
        "(media_item_id, target_kind, target_id, started_at) "
        "VALUES (?, ?, ?, ?)",
        (media_item_id, target_kind, str(target_id), now),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _complete_delete_intent(conn: sqlite3.Connection, intent_id: int) -> None:
    """Mark a delete intent as successfully completed."""
    conn.execute(
        "UPDATE delete_intents SET completed_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), intent_id),
    )
    conn.commit()


def _fail_delete_intent(conn: sqlite3.Connection, intent_id: int, error: str) -> None:
    """Record the last error on a delete intent (intent remains pending)."""
    conn.execute(
        "UPDATE delete_intents SET last_error = ? WHERE id = ?",
        (str(error)[:2000], intent_id),
    )
    conn.commit()


def reconcile_pending_delete_intents() -> int:
    """Find unresolved delete intents and attempt to complete their cleanup.

    This function is exposed for wiring into bootstrap / startup.  It does not
    run automatically — a follow-up commit is required to call it from
    ``main.py`` or the bootstrap module at process start-up.

    Returns the number of intents resolved during this call.
    """
    conn = get_db()
    pending = conn.execute(
        "SELECT id, media_item_id, target_kind, target_id "
        "FROM delete_intents WHERE completed_at IS NULL"
    ).fetchall()

    resolved = 0
    for row in pending:
        intent_id = row["id"]
        media_item_id = row["media_item_id"]

        # If the media_items row is already gone the external call must have
        # succeeded — just mark the intent complete.
        item_exists = conn.execute(
            "SELECT id FROM media_items WHERE id = ?", (media_item_id,)
        ).fetchone()
        if item_exists is None:
            _complete_delete_intent(conn, intent_id)
            resolved += 1
            logger.info(
                "delete_intent.reconciled intent_id=%s media_id=%s reason=already_gone",
                intent_id,
                media_item_id,
            )
            continue

        # Media row still exists — clean it up idempotently.
        try:
            conn.execute("BEGIN IMMEDIATE")
            log_audit(conn, media_item_id, "deleted", "Reconciled by startup cleanup")
            conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_item_id,))
            conn.execute("DELETE FROM media_items WHERE id = ?", (media_item_id,))
            conn.execute("COMMIT")
            _complete_delete_intent(conn, intent_id)
            resolved += 1
            logger.info(
                "delete_intent.reconciled intent_id=%s media_id=%s reason=cleanup_on_startup",
                intent_id,
                media_item_id,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            _fail_delete_intent(conn, intent_id, str(exc))
            logger.warning(
                "delete_intent.reconcile_failed intent_id=%s media_id=%s error=%s",
                intent_id,
                media_item_id,
                exc,
                exc_info=True,
            )

    return resolved


@router.post("/api/media/{media_id}/delete")
def api_media_delete(
    media_id: str,
    request: Request,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Delete a media item via Radarr/Sonarr.

    Two-phase, three-transaction layout (finding 11) — intentionally split:

    1. **Snapshot transaction** (``BEGIN IMMEDIATE`` … ``COMMIT``)
       — read the media row, capture identifiers, release the lock.
    2. **External Arr call** (no DB transaction held)
       — Radarr / Sonarr round-trip can take seconds; holding a SQLite
       write lock that long would block every other writer in the
       process. A delete-intent row is persisted *before* this step so
       a crash between the Arr call returning and the DB cleanup
       landing can be reconciled by ``reconcile_pending_delete_intents``
       at startup.
    3. **Cleanup transaction** (``BEGIN IMMEDIATE`` … ``COMMIT``)
       — write the audit row, prune ``scheduled_actions``, drop the
       ``media_items`` row, then mark the delete-intent complete.

    The recovery path handles every observable partial state.

    Note: ``build_radarr_from_db`` and ``build_sonarr_from_db`` are
    looked up via the sibling ``.api`` module so that test patches on
    ``mediaman.web.routes.library.api.build_*`` continue to take effect.
    """
    # Lazy import to break the cycle (api -> _intent re-export, _intent
    # -> api lookup at call time only). Importing inside the handler
    # also means tests can patch the attribute on ``api`` and we always
    # honour the latest binding.
    from . import api as _api

    if not _DELETE_LIMITER.check(username):
        logger.warning("media.delete_throttled user=%s", username)
        return JSONResponse(
            {"error": "Too many delete operations — slow down"},
            status_code=429,
        )
    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, title, media_type, file_path, file_size_bytes, radarr_id, sonarr_id, season_number, plex_rating_key "
            "FROM media_items WHERE id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            # Finding 12: do NOT leak existence/non-existence here.
            # Returning 404 told an attacker which media IDs were
            # valid; 403 is uniform across "row is missing" and
            # "row exists but the actor isn't the owner" (auth has
            # already been confirmed by the dependency above).
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        snapshot = {
            "title": row["title"],
            "media_type": row["media_type"],
            "file_path": row["file_path"],
            "file_size_bytes": row["file_size_bytes"],
            "radarr_id": row["radarr_id"],
            "sonarr_id": row["sonarr_id"],
            "season_number": row["season_number"],
            "plex_rating_key": row["plex_rating_key"],
        }
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
        raise

    title = snapshot["title"]
    config = request.app.state.config
    is_movie = snapshot["media_type"] == "movie"

    def _is_already_gone(exc: Exception) -> bool:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
        return status == 404

    # Tracks the row id of the delete-intent persisted before any external
    # call. ``None`` means there is no intent to finalise (the no-Arr-id
    # path skips writing one — finding 13).
    intent_id: int | None = None

    if is_movie:
        client = _api.build_radarr_from_db(conn, config.secret_key)
        if client:
            radarr_id = snapshot["radarr_id"]
            if radarr_id:
                # Persist intent before the external call so a crash is recoverable.
                intent_id = _record_delete_intent(conn, media_id, "radarr", radarr_id)
                try:
                    client.delete_movie(radarr_id)
                    logger.info(
                        "Deleted '%s' via Radarr (id %s, with files + exclusion)", title, radarr_id
                    )
                except Exception as exc:
                    if _is_already_gone(exc):
                        logger.info(
                            "Radarr reports id %s already gone for '%s' — idempotent delete",
                            radarr_id,
                            title,
                        )
                    else:
                        _fail_delete_intent(conn, intent_id, str(exc))
                        logger.warning(
                            "Radarr delete failed for '%s': %s", title, exc, exc_info=True
                        )
                        return JSONResponse(
                            {
                                "ok": False,
                                "error": "Upstream Radarr delete failed — DB row preserved",
                            },
                            status_code=502,
                        )
            else:
                # No external call to make → no recovery scenario, so
                # don't write a placeholder intent row (finding 13).
                logger.info(
                    "No stored radarr_id for '%s' — skipping Radarr-level delete.",
                    title,
                )
    else:
        client = _api.build_sonarr_from_db(conn, config.secret_key)
        if client:
            sid = snapshot["sonarr_id"]
            season_num = snapshot["season_number"]
            if sid and season_num is not None:
                intent_id = _record_delete_intent(conn, media_id, "sonarr", sid)
                try:
                    client.delete_episode_files(sid, season_num)
                    client.unmonitor_season(sid, season_num)
                    logger.info("Deleted season files for '%s' S%s via Sonarr", title, season_num)
                    if not client.has_remaining_files(sid):
                        client.delete_series(sid)
                        logger.info(
                            "No files remain for '%s' — deleted series from Sonarr with exclusion",
                            title,
                        )
                except Exception as exc:
                    if _is_already_gone(exc):
                        logger.info(
                            "Sonarr reports id %s already gone for '%s' — idempotent delete",
                            sid,
                            title,
                        )
                    else:
                        _fail_delete_intent(conn, intent_id, str(exc))
                        logger.warning(
                            "Sonarr delete failed for '%s': %s", title, exc, exc_info=True
                        )
                        return JSONResponse(
                            {
                                "ok": False,
                                "error": "Upstream Sonarr delete failed — DB row preserved",
                            },
                            status_code=502,
                        )
            # If no sid/season_num, skip the placeholder intent (finding 13).

    rk = snapshot["plex_rating_key"] or ""
    detail = f"Deleted '{title}' by {username}"
    if rk:
        detail += f" [rk:{rk}]"
    try:
        conn.execute("BEGIN IMMEDIATE")
        log_audit(
            conn,
            media_id,
            "deleted",
            detail,
            space_bytes=snapshot["file_size_bytes"],
            actor=username,
        )
        conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_id,))
        conn.execute("DELETE FROM media_items WHERE id = ?", (media_id,))
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
        raise

    if intent_id is not None:
        _complete_delete_intent(conn, intent_id)
    logger.info("Deleted %s (%s) — %s by %s", media_id, title, snapshot["file_path"], username)
    return JSONResponse({"ok": True, "id": media_id})
