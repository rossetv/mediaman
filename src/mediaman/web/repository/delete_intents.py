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


def _reconcile_one_intent(
    conn: sqlite3.Connection, intent_id: int, media_item_id: str, last_error: str | None
) -> bool:
    """Reconcile a single pending intent.  Returns ``True`` on resolution."""
    from mediaman.core.audit import log_audit

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
    # rationale: cold-start — reconciler runs at startup and
    # processes the whole intent backlog; every error must roll back the
    # current intent's transaction and leave it pending (without an error,
    # which would mark it as a failed Arr call) so the next startup retries
    # without crashing the boot sequence.
    try:
        with conn:
            log_audit(conn, media_item_id, "deleted", "Reconciled by startup cleanup")
            conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_item_id,))
            conn.execute("DELETE FROM media_items WHERE id = ?", (media_item_id,))
        _complete_delete_intent(conn, intent_id)
        logger.info(
            "delete_intent.reconciled intent_id=%s media_id=%s reason=cleanup_on_startup",
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


def reconcile_pending_delete_intents(conn: sqlite3.Connection) -> int:
    """Find unresolved delete intents and attempt to complete their cleanup.

    Accepts an explicit connection rather than acquiring one via
    ``get_db()`` so startup wiring can pass the connection it already
    holds and tests can use a fixture connection.

    Returns the number of intents resolved during this call.
    """
    pending = conn.execute(
        "SELECT id, media_item_id, last_error FROM delete_intents WHERE completed_at IS NULL"
    ).fetchall()
    resolved = 0
    for row in pending:
        if _reconcile_one_intent(conn, row["id"], row["media_item_id"], row["last_error"]):
            resolved += 1
    return resolved
