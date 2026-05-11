"""Repository functions for delete-intent durability rows.

The ``delete_intents`` table is a journal used to detect crashes that land
between the external Radarr/Sonarr delete call and the local DB cleanup
transaction. Every write here is committed immediately so a process kill
between record and cleanup leaves a recoverable trail for
:func:`reconcile_pending_delete_intents` on the next start.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3

from mediaman.core.audit import log_audit
from mediaman.core.time import now_iso
from mediaman.db import get_db

logger = logging.getLogger(__name__)


def _record_delete_intent(
    conn: sqlite3.Connection,
    media_item_id: str,
    target_kind: str,
    target_id: str | int,
) -> int:
    """Insert a delete intent row and return its ``id``.

    Must be called *before* the external Radarr/Sonarr delete so that a
    crash between the external call and the local DB cleanup can be
    detected and reconciled on startup via
    :func:`reconcile_pending_delete_intents`.
    """
    now = now_iso()
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
        (now_iso(), intent_id),
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

    This function is exposed for wiring into bootstrap / startup.  It does
    not run automatically — call it from ``main.py`` or the bootstrap module
    at process start-up.

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
