"""Delete-intent durability helpers.

The intent log opens before the external Arr call and closes after the
local DB cleanup so a crash between those two events can be reconciled
at startup.  The internal helpers here
(`_record_delete_intent`, `_complete_delete_intent`,
`_fail_delete_intent`) are used directly by the route layer in
`mediaman.web.routes.library_api`; the startup reconciler is wired
in `mediaman.app_factory`.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from mediaman.core.time import now_utc

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
        if verify_gone is not None and verify_gone(intent):
            with conn:
                log_audit(conn, media_item_id, "deleted", "Confirmed gone by Radarr/Sonarr")
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
        # Cut off before the Arr confirmed anything: the files may remain,
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
    conn: sqlite3.Connection, verify_gone: VerifyGone | None = None
) -> int:
    """Find unresolved delete intents and close each one.

    An interrupted intent whose item the optional *verify_gone* cannot
    confirm gone is closed as failed; the media row stays.

    Takes an explicit connection so the caller can give it one owned by
    the thread it runs on.

    Returns the number of intents resolved during this call.
    """
    pending = conn.execute(
        "SELECT id, media_item_id, target_kind, target_id, last_error "
        "FROM delete_intents WHERE completed_at IS NULL"
    ).fetchall()
    resolved = 0
    for row in pending:
        if _reconcile_one_intent(conn, row, verify_gone):
            resolved += 1
    return resolved
