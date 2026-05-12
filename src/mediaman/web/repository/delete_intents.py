"""Delete-intent durability helpers.

The intent log opens before the external Arr call and closes after the
local DB cleanup so a crash between those two events can be reconciled
at startup.  The internal helpers here
(:func:`_record_delete_intent`, :func:`_complete_delete_intent`,
:func:`_fail_delete_intent`) are used directly by the route layer in
:mod:`mediaman.web.routes.library_api`; the startup reconciler is wired
in :mod:`mediaman.app_factory`.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from mediaman.db import get_db

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
    import contextlib

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
    # rationale: §6.4 site 4 (cold-start) — reconciler runs at startup and
    # processes the whole intent backlog; every error must roll back the
    # current intent's transaction and record the failure so the next
    # startup retries without crashing the boot sequence.
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
