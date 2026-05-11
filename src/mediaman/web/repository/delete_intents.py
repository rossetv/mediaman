"""Delete-intent durability helpers and per-service delete handlers.

The intent log opens before the external Arr call and closes after the
local DB cleanup so a crash between those two events can be reconciled
at startup.  The per-service handlers
(:func:`handle_radarr_delete` / :func:`handle_sonarr_delete`) share the
``intent_id`` thread with :func:`finalise_media_delete` via the
``api_media_delete`` orchestrator in :mod:`__init__`.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from fastapi.responses import JSONResponse

from mediaman.core.audit import log_audit
from mediaman.db import get_db
from mediaman.web.responses import respond_err

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient

logger = logging.getLogger(__name__)


class MediaDeleteSnapshot(TypedDict):
    """Frozen view of a ``media_items`` row, captured before the Arr round-trip."""

    title: str
    media_type: str
    file_path: str | None
    file_size_bytes: int | None
    radarr_id: str | int | None
    sonarr_id: str | int | None
    season_number: int | None
    plex_rating_key: str | None


def _record_delete_intent(
    conn: sqlite3.Connection,
    media_item_id: str,
    target_kind: str,
    target_id: str,
) -> int:
    """Insert a delete intent row and return its ``id``.

    Must be called *before* the external Radarr/Sonarr delete so that a
    crash between the external call and the local DB cleanup can be
    detected and reconciled on startup via
    :func:`reconcile_pending_delete_intents`.
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


def _reconcile_one_intent(conn: sqlite3.Connection, intent_id: int, media_item_id: str) -> bool:
    """Reconcile a single pending intent.  Returns ``True`` on resolution."""
    item_exists = conn.execute(
        "SELECT id FROM media_items WHERE id = ?", (media_item_id,)
    ).fetchone()
    if item_exists is None:
        # External call must have succeeded — mark intent complete.
        _complete_delete_intent(conn, intent_id)
        logger.info(
            "delete_intent.reconciled intent_id=%s media_id=%s reason=already_gone",
            intent_id,
            media_item_id,
        )
        return True
    try:
        conn.execute("BEGIN IMMEDIATE")
        log_audit(conn, media_item_id, "deleted", "Reconciled by startup cleanup")
        conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_item_id,))
        conn.execute("DELETE FROM media_items WHERE id = ?", (media_item_id,))
        conn.execute("COMMIT")
        _complete_delete_intent(conn, intent_id)
        logger.info(
            "delete_intent.reconciled intent_id=%s media_id=%s reason=cleanup_on_startup",
            intent_id,
            media_item_id,
        )
        return True
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
        return False


def reconcile_pending_delete_intents() -> int:
    """Find unresolved delete intents and attempt to complete their cleanup.

    Exposed for wiring into bootstrap / startup.  Returns the number of
    intents resolved during this call.
    """
    conn = get_db()
    pending = conn.execute(
        "SELECT id, media_item_id, target_kind, target_id "
        "FROM delete_intents WHERE completed_at IS NULL"
    ).fetchall()
    resolved = 0
    for row in pending:
        if _reconcile_one_intent(conn, row["id"], row["media_item_id"]):
            resolved += 1
    return resolved


def _is_already_gone(exc: Exception) -> bool:
    """Return True when *exc* is a 404 response — the Arr record is already gone."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else None
    return status == 404


def snapshot_media_for_delete(
    conn: sqlite3.Connection, media_id: str
) -> tuple[MediaDeleteSnapshot | None, JSONResponse | None]:
    """Read the media row inside a write transaction and return a snapshot dict.

    Returns ``(snapshot, None)`` on success or ``(None, error_response)``
    when the row is absent.  Treats "not found" as 403 rather than 404
    so the endpoint cannot be used as an existence oracle.
    """
    _not_found = False
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, title, media_type, file_path, file_size_bytes, radarr_id, "
                "sonarr_id, season_number, plex_rating_key "
                "FROM media_items WHERE id = ?",
                (media_id,),
            ).fetchone()
            if row is None:
                _not_found = True
                raise RuntimeError("not_found")  # triggers with-block rollback
            snapshot = MediaDeleteSnapshot(
                title=row["title"],
                media_type=row["media_type"],
                file_path=row["file_path"],
                file_size_bytes=row["file_size_bytes"],
                radarr_id=row["radarr_id"],
                sonarr_id=row["sonarr_id"],
                season_number=row["season_number"],
                plex_rating_key=row["plex_rating_key"],
            )
    except RuntimeError:
        if _not_found:
            return None, respond_err("forbidden", status=403)
        raise
    return snapshot, None


def handle_radarr_delete(
    conn: sqlite3.Connection,
    client: ArrClient,
    media_id: str,
    snapshot: MediaDeleteSnapshot,
) -> tuple[int | None, JSONResponse | None]:
    """Run the Radarr-side delete for a movie snapshot.

    Returns ``(intent_id, None)`` on success (or a no-op when the snapshot
    has no ``radarr_id``) and ``(intent_id, error_response)`` on a
    non-404 failure.  A 404 is treated as idempotent success.
    """
    radarr_id = snapshot["radarr_id"]
    title = snapshot["title"]
    if not radarr_id:
        logger.info("No stored radarr_id for '%s' — skipping Radarr-level delete.", title)
        return None, None
    intent_id = _record_delete_intent(conn, media_id, "radarr", str(radarr_id))
    try:
        client.delete_movie(int(radarr_id))
        logger.info("Deleted '%s' via Radarr (id %s, with files + exclusion)", title, radarr_id)
    except Exception as exc:
        if _is_already_gone(exc):
            logger.info(
                "Radarr reports id %s already gone for '%s' — idempotent delete",
                radarr_id,
                title,
            )
            return intent_id, None
        _fail_delete_intent(conn, intent_id, str(exc))
        logger.warning("Radarr delete failed for '%s': %s", title, exc, exc_info=True)
        return intent_id, respond_err(
            "upstream_delete_failed",
            status=502,
            message="Upstream Radarr delete failed — DB row preserved",
        )
    return intent_id, None


def handle_sonarr_delete(
    conn: sqlite3.Connection,
    sonarr_client: ArrClient,
    media_id: str,
    snapshot: MediaDeleteSnapshot,
) -> tuple[int | None, JSONResponse | None]:
    """Run the Sonarr-side delete for a season snapshot.

    Deletes season files, unmonitors the season, and removes the series
    entirely when no episode files remain.  A 404 from any step is
    treated as idempotent success.
    """
    sid = snapshot["sonarr_id"]
    season_num = snapshot["season_number"]
    title = snapshot["title"]
    if not sid or season_num is None:
        return None, None
    intent_id = _record_delete_intent(conn, media_id, "sonarr", str(sid))
    try:
        sid_int = int(sid)
        sonarr_client.delete_episode_files(sid_int, season_num)
        sonarr_client.unmonitor_season(sid_int, season_num)
        logger.info("Deleted season files for '%s' S%s via Sonarr", title, season_num)
        if not sonarr_client.has_remaining_files(sid_int):
            sonarr_client.delete_series(sid_int)
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
            return intent_id, None
        _fail_delete_intent(conn, intent_id, str(exc))
        logger.warning("Sonarr delete failed for '%s': %s", title, exc, exc_info=True)
        return intent_id, respond_err(
            "upstream_delete_failed",
            status=502,
            message="Upstream Sonarr delete failed — DB row preserved",
        )
    return intent_id, None


def finalise_media_delete(
    conn: sqlite3.Connection,
    media_id: str,
    snapshot: MediaDeleteSnapshot,
    username: str,
) -> None:
    """Run the cleanup transaction: audit row + scheduled-actions purge + row drop."""
    title = snapshot["title"]
    rk = snapshot["plex_rating_key"] or ""
    detail = f"Deleted '{title}' by {username}"
    if rk:
        detail += f" [rk:{rk}]"
    with conn:
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
