"""Delete-intent durability helpers.

The intent log opens before the external Arr call and closes after the
local DB cleanup so a crash between those two events can be reconciled
at startup.  The internal helpers here
(`_record_delete_intent`, `_complete_delete_intent`,
`_fail_delete_intent`) are used directly by the route layer in
`mediaman.web.routes.library_api`; the startup reconciler and its
`build_verify_gone` check are wired in `mediaman.app_factory`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable

from mediaman.core.deleted_media import record_deleted_media
from mediaman.core.time import now_utc
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.web.repository.library_api import _parse_tracked_files

logger = logging.getLogger(__name__)


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
    `reconcile_pending_delete_intents`.
    """
    now = now_utc().isoformat()
    with conn:
        cur = conn.execute(
            "INSERT INTO delete_intents "
            "(media_item_id, target_kind, target_id, started_at) "
            "VALUES (?, ?, ?, ?)",
            (media_item_id, target_kind, str(target_id), now),
        )
    return cur.lastrowid  # type: ignore[return-value]


def _complete_delete_intent(conn: sqlite3.Connection, intent_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE delete_intents SET completed_at = ? WHERE id = ?",
            (now_utc().isoformat(), intent_id),
        )


def _fail_delete_intent(
    conn: sqlite3.Connection,
    intent_id: int,
    media_item_id: str,
    detail: str,
    error: str,
    *,
    actor: str | None = None,
) -> None:
    """Close a delete intent whose Radarr/Sonarr call failed, and audit the failure.

    The intent is completed with ``last_error`` set so the startup reconcile
    never books the item as deleted: the Arr did not confirm it gone, so the
    media row and its scheduled actions stay for the operator to retry.
    """
    from mediaman.core.audit import log_audit

    with conn:
        log_audit(conn, media_item_id, "delete_failed", detail, actor=actor)
        conn.execute(
            "UPDATE delete_intents SET completed_at = ?, last_error = ? WHERE id = ?",
            (now_utc().isoformat(), str(error)[:2000], intent_id),
        )


_INTERRUPTED_DETAIL = "Delete interrupted; not confirmed by Radarr/Sonarr — press Delete again"

VerifyGone = Callable[[sqlite3.Row], bool | None]
"""Asks Radarr/Sonarr whether an intent's item is gone: True, False, or None when unknown."""


def _is_not_found(exc: Exception) -> bool:
    """True when a Radarr/Sonarr error carries a 404, on a SafeHTTPError or any
    exception with ``response.status_code``.
    """
    if getattr(exc, "status_code", None) == 404:
        return True
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 404


def build_verify_gone(conn: sqlite3.Connection, secret_key: str) -> VerifyGone:
    """Return a `VerifyGone` that asks the Radarr/Sonarr configured in the settings.

    Only a 404 for the Radarr movie or Sonarr series proves the item gone.
    ``hasFile: false`` or an empty episode-file list is not proof: Radarr and
    Sonarr report that too when their storage mount is down. An entry still
    listed answers ``False``; an error asking answers ``None``.
    """

    def entry_gone(fetch: Callable[[], object]) -> bool:
        try:
            fetch()
        except Exception as exc:
            if _is_not_found(exc):
                return True
            raise
        return False

    def radarr_gone(target_id: int) -> bool | None:
        client = build_radarr_from_db(conn, secret_key)
        if client is None:
            return None
        return entry_gone(lambda: client.get_movie_by_id(target_id))

    def sonarr_gone(target_id: int) -> bool | None:
        client = build_sonarr_from_db(conn, secret_key)
        if client is None:
            return None
        return entry_gone(lambda: client.get_series_by_id(target_id))

    def verify_gone(intent: sqlite3.Row) -> bool | None:
        # rationale: a reconcile must never book an item deleted on doubt;
        # any failure to ask Radarr/Sonarr counts as "cannot tell".
        try:
            target_id = int(intent["target_id"])
            if intent["target_kind"] == "radarr":
                return radarr_gone(target_id)
            if intent["target_kind"] == "sonarr":
                return sonarr_gone(target_id)
        except Exception as exc:
            logger.warning("delete_intent.verify_failed intent_id=%s error=%s", intent["id"], exc)
        return None

    return verify_gone


def _tracked_file_on_disk(conn: sqlite3.Connection, media_item_id: str) -> bool:
    """True when any file tracked for the item still exists locally.

    The Arr answering 404 does not prove its files are gone from mediaman's
    storage: a delete cut short while waiting for them to vanish leaves them.
    """
    row = conn.execute(
        "SELECT tracked_files FROM media_items WHERE id = ?", (media_item_id,)
    ).fetchone()
    entries = _parse_tracked_files(row["tracked_files"]) if row else None
    return any(
        os.path.lexists(path)
        for entry in entries or ()
        if isinstance(path := entry.get("path"), str) and path
    )


def _reconcile_one_intent(
    conn: sqlite3.Connection,
    intent: sqlite3.Row,
    verify_gone: VerifyGone | None = None,
) -> bool:
    """Reconcile a single pending intent.  Returns ``True`` on resolution."""
    from mediaman.core.audit import log_audit

    intent_id = intent["id"]
    media_item_id = intent["media_item_id"]
    last_error = intent["last_error"]
    item = conn.execute(
        "SELECT title, media_type, tmdb_id, tvdb_id, season_number FROM media_items WHERE id = ?",
        (media_item_id,),
    ).fetchone()
    if item is None:
        # External call must have succeeded — mark intent complete.
        _complete_delete_intent(conn, intent_id)
        logger.info(
            "delete_intent.reconciled intent_id=%s media_id=%s reason=already_gone",
            intent_id,
            media_item_id,
        )
        return True
    if last_error is not None:
        # A pending intent carrying an error is a failed Arr call: the Arr
        # never confirmed the delete, so the item must not be booked gone.
        _fail_delete_intent(
            conn,
            intent_id,
            media_item_id,
            "Delete not confirmed by Radarr/Sonarr; closed by startup cleanup",
            last_error,
        )
        logger.info(
            "delete_intent.reconciled intent_id=%s media_id=%s reason=upstream_failed",
            intent_id,
            media_item_id,
        )
        return True
    # rationale: cold-start — the reconciler processes the whole intent
    # backlog; every error must roll back the current intent's transaction
    # and leave it pending so the next run retries without aborting the rest.
    try:
        if (
            verify_gone is not None
            and verify_gone(intent)
            and not _tracked_file_on_disk(conn, media_item_id)
        ):
            with conn:
                audit_id = log_audit(
                    conn, media_item_id, "deleted", "Confirmed gone by Radarr/Sonarr"
                )
                record_deleted_media(
                    conn,
                    audit_id,
                    media_type=item["media_type"],
                    tmdb_id=item["tmdb_id"],
                    tvdb_id=item["tvdb_id"],
                    season_number=item["season_number"],
                    title=item["title"],
                )
                conn.execute(
                    "DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_item_id,)
                )
                conn.execute("DELETE FROM media_items WHERE id = ?", (media_item_id,))
            _complete_delete_intent(conn, intent_id)
            logger.info(
                "delete_intent.reconciled intent_id=%s media_id=%s reason=verified_gone",
                intent_id,
                media_item_id,
            )
            return True
        # Cut off before the Arr confirmed anything, or its files remain locally:
        # so keep the row for the operator to delete again.
        _fail_delete_intent(
            conn, intent_id, media_item_id, _INTERRUPTED_DETAIL, _INTERRUPTED_DETAIL
        )
        logger.info(
            "delete_intent.reconciled intent_id=%s media_id=%s reason=interrupted",
            intent_id,
            media_item_id,
        )
        return True
    except Exception as exc:
        logger.warning(
            "delete_intent.reconcile_failed intent_id=%s media_id=%s error=%s",
            intent_id,
            media_item_id,
            exc,
            exc_info=True,
        )
        return False


def reconcile_pending_delete_intents(
    conn: sqlite3.Connection,
    verify_gone: VerifyGone | None = None,
    *,
    started_before: str | None = None,
) -> int:
    """Find unresolved delete intents and close each one.

    An interrupted intent whose item the optional *verify_gone* cannot
    confirm gone is closed as failed; the media row stays.

    With *started_before* (an ISO timestamp as ``now_utc().isoformat()``
    writes it) only intents started earlier are touched, so a run that
    overlaps live requests leaves their in-flight deletes alone.

    Takes an explicit connection so the caller can give it one owned by
    the thread it runs on.

    Returns the number of intents resolved during this call.
    """
    sql = (
        "SELECT id, media_item_id, target_kind, target_id, last_error "
        "FROM delete_intents WHERE completed_at IS NULL"
    )
    params: tuple[str, ...] = ()
    if started_before is not None:
        sql += " AND started_at < ?"
        params = (started_before,)
    pending = conn.execute(sql, params).fetchall()
    resolved = 0
    for row in pending:
        if _reconcile_one_intent(conn, row, verify_gone):
            resolved += 1
    return resolved
