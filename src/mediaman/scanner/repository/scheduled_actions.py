"""SQL operations on the `scheduled_actions`, `kept_shows`, and `snoozes` tables."""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from mediaman.audit import log_audit
from mediaman.core.time import now_iso

logger = logging.getLogger("mediaman")

# The action that means deletion is already lined up.
DELETION_ACTION = "scheduled_deletion"

# Default token TTL: 30 days from now.
_TOKEN_TTL_DAYS = 30


# ---------------------------------------------------------------------------
# scheduled_actions — protection / schedule queries
# ---------------------------------------------------------------------------


def is_protected(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if the item has an active protection action.

    An item is protected if **any** of its ``scheduled_actions`` rows
    has ``action='protected_forever'`` (regardless of ``token_used``),
    or if **any** of its ``snoozed`` rows still has ``execute_at`` in
    the future.

    The previous implementation used ``ORDER BY id DESC LIMIT 1`` to
    pick a single "latest" row, which gave the wrong answer whenever
    a higher-id row contradicted a still-authoritative lower-id one
    (Domain 05 finding): an earlier ``protected_forever`` row could be
    masked by a later expired ``snoozed`` row, falsely reporting the
    item as unprotected and queuing it for deletion. The schema does
    not enforce one-row-per-item, so we must not rely on row order —
    we check the two protective states explicitly instead.
    """
    # protected_forever wins over everything: ignore execute_at and
    # token_used here. If even one such row exists, the item is kept.
    if (
        conn.execute(
            "SELECT 1 FROM scheduled_actions "
            "WHERE media_item_id = ? AND action = 'protected_forever' LIMIT 1",
            (media_id,),
        ).fetchone()
        is not None
    ):
        return True
    # No protected_forever row — fall back to active snoozes.
    now = now_iso()
    return (
        conn.execute(
            "SELECT 1 FROM scheduled_actions "
            "WHERE media_item_id = ? AND action = 'snoozed' "
            "AND execute_at IS NOT NULL AND execute_at > ? "
            "LIMIT 1",
            (media_id, now),
        ).fetchone()
        is not None
    )


def is_already_scheduled(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if deletion is already pending for this item."""
    row = conn.execute(
        """
        SELECT id FROM scheduled_actions
        WHERE media_item_id = ? AND action = 'scheduled_deletion' AND token_used = 0
        LIMIT 1
        """,
        (media_id,),
    ).fetchone()
    return row is not None


def has_expired_snooze(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if the item has a prior snoozed action that was consumed."""
    row = conn.execute(
        """
        SELECT id FROM scheduled_actions
        WHERE media_item_id = ? AND action = 'snoozed' AND token_used = 1
        LIMIT 1
        """,
        (media_id,),
    ).fetchone()
    return row is not None


def _is_show_kept_pure(
    conn: sqlite3.Connection,
    show_rating_key: str | None,
    *,
    now_iso_str: str | None = None,
) -> bool:
    """Pure read for ``is_show_kept``: returns True iff a live keep rule exists.

    Performs **no** writes. Used directly by callers that want a clean
    boolean answer without touching the DB; :func:`is_show_kept` wraps
    this with the legacy expired-snooze cleanup to preserve the
    pre-existing engine.py contract.
    """
    if not show_rating_key:
        return False
    now = now_iso_str or now_iso()
    row = conn.execute(
        """
        SELECT action, execute_at FROM kept_shows
        WHERE show_rating_key = ?
        LIMIT 1
        """,
        (show_rating_key,),
    ).fetchone()
    if row is None:
        return False
    if row["action"] == "protected_forever":
        return True
    return bool(row["execute_at"] and row["execute_at"] > now)


def cleanup_expired_show_snoozes(conn: sqlite3.Connection, now_iso: str) -> int:
    """Delete every ``kept_shows`` row whose snoozed keep has lapsed.

    ``protected_forever`` rows have ``execute_at IS NULL`` and are
    therefore left untouched. Returns the row count removed so callers
    can log / metric the cleanup.

    Pulled out of :func:`is_show_kept` so the cleanup is a separately
    callable, observable, single-purpose operation. Suitable for a
    periodic scheduler job — a single statement covers every expired
    row in one round trip — but :func:`is_show_kept` still calls it
    inline for back-compat with the engine.py caller that expects the
    legacy "ask + clean" behaviour.
    """
    cur = conn.execute(
        "DELETE FROM kept_shows "
        "WHERE action = 'snoozed' "
        "AND execute_at IS NOT NULL "
        "AND execute_at <= ?",
        (now_iso,),
    )
    return cur.rowcount or 0


def is_show_kept(conn: sqlite3.Connection, show_rating_key: str | None) -> bool:
    """Return True if the show has an active keep rule in ``kept_shows``.

    Composed from two single-purpose helpers (Domain 05 finding):

    * :func:`_is_show_kept_pure` — the read.
    * :func:`cleanup_expired_show_snoozes` — the cleanup.

    The split makes each side observable in isolation. This top-level
    function preserves the legacy "ask + clean" contract for the
    existing engine.py caller: when the read says the keep is no
    longer live (i.e. an expired snooze), we sweep the row out so the
    table doesn't accrete dead rows over time. Callers that want a
    pure read should call :func:`_is_show_kept_pure` directly.
    """
    if not show_rating_key:
        return False
    now = now_iso()
    kept = _is_show_kept_pure(conn, show_rating_key, now_iso_str=now)
    if not kept:
        # Either the row is missing or its snooze has lapsed; ask the
        # cleanup helper to remove any expired row for this key. Doing
        # the cleanup at most once per call (and only on the no-longer-
        # kept path) keeps the read fast for the common protected_forever
        # case.
        cleanup_expired_show_snoozes(conn, now)
    return kept


# ---------------------------------------------------------------------------
# scheduled_actions — mutations
# ---------------------------------------------------------------------------


def schedule_deletion(
    conn: sqlite3.Connection,
    *,
    media_id: str,
    is_reentry: bool,
    grace_days: int,
    secret_key: str,
) -> str:
    """Insert a scheduled_deletion row and write an audit entry.

    Returns the literal ``"scheduled"`` on success, or ``"skipped"`` when
    a concurrent scanner has already inserted an active deletion for the
    same ``media_id`` (the migration-25 partial unique index raises
    ``IntegrityError``). The skipped path is the desired outcome — the
    other run already lined the deletion up — so we swallow the error
    and report it as a clean skip rather than letting it bubble up as a
    500.

    Uses a unique random placeholder token for the initial insert so
    the ``token`` unique index can't collide between concurrent scheduler
    runs, then swaps in the real HMAC-signed keep token once we know the
    row id.
    """
    now = datetime.now(UTC)
    execute_at = now + timedelta(days=grace_days)
    expires_at = int((now + timedelta(days=_TOKEN_TTL_DAYS)).timestamp())

    # Finding 16: use a placeholder for the initial insert (satisfies
    # any remaining NOT NULL constraint on legacy schemas before migration 28).
    # After migration 28 the token column is nullable so this placeholder
    # is only needed as a uniqueness sentinel.
    placeholder = f"pending-{secrets.token_urlsafe(16)}"

    try:
        cursor = conn.execute(
            """
            INSERT INTO scheduled_actions
                (media_item_id, action, scheduled_at, execute_at, token, token_used, is_reentry)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                media_id,
                DELETION_ACTION,
                now.isoformat(),
                execute_at.isoformat(),
                placeholder,
                1 if is_reentry else 0,
            ),
        )
    except sqlite3.IntegrityError:
        # Either the partial unique index ``idx_scheduled_actions_unique_active_deletion``
        # (migration 25) already has an active pending deletion for this
        # item, or the rare ``token``/``token_hash`` placeholder collision
        # tripped a unique index. Both cases mean "another concurrent run
        # already covered this item" — there's nothing more to do.
        logger.info(
            "repository.schedule_deletion.skip media_id=%s reason=integrity_error",
            media_id,
        )
        return "skipped"
    action_id = cursor.lastrowid
    # ``lastrowid`` is typed as ``int | None``; SQLite always populates it
    # after a successful INSERT against an INTEGER PRIMARY KEY table.
    assert action_id is not None

    # Lazy imports: keep generate_keep_token and hashlib out of the module-
    # level dependency graph so this module remains a pure SQL layer.  The
    # production scan path uses phases.upsert.schedule_deletion instead;
    # this function is kept for back-compat with tests and ad-hoc callers.
    import hashlib as _hashlib

    from mediaman.crypto import generate_keep_token as _generate_keep_token

    token = _generate_keep_token(
        media_item_id=media_id,
        action_id=action_id,
        expires_at=expires_at,
        secret_key=secret_key,
    )

    token_hash = _hashlib.sha256(token.encode()).hexdigest()
    # Finding 16: write only the hash; null out the raw token.  On pre-
    # migration-28 schemas the token column is NOT NULL, so we write the
    # hash and leave the placeholder in place — migration 28 will clear it.
    # On migration-28+ schemas (token is nullable) we clear the raw token.
    try:
        conn.execute(
            "UPDATE scheduled_actions SET token_hash = ?, token = NULL WHERE id = ?",
            (token_hash, action_id),
        )
    except Exception:
        # Pre-migration-28: token column is NOT NULL; just write the hash.
        conn.execute(
            "UPDATE scheduled_actions SET token_hash = ? WHERE id = ?",
            (token_hash, action_id),
        )

    log_audit(
        conn,
        media_id,
        DELETION_ACTION,
        "scheduled by scan engine" + (" (re-entry)" if is_reentry else ""),
    )
    return "scheduled"


def fetch_stuck_deletions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return rows in ``scheduled_actions`` still marked ``deleting``.

    Returns an empty list if the ``delete_status`` column has not been
    migrated yet (older DB schemas).
    """
    try:
        return conn.execute(
            "SELECT sa.id, sa.media_item_id, sa.action, mi.file_path, "
            "mi.file_size_bytes, mi.title, mi.plex_rating_key "
            "FROM scheduled_actions sa "
            "LEFT JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.delete_status = 'deleting'"
        ).fetchall()
    except sqlite3.OperationalError:
        # delete_status column not yet migrated — nothing to do.
        return []


def fetch_pending_deletions(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    """Return all pending deletions whose grace period has elapsed."""
    return conn.execute(
        "SELECT sa.id, sa.media_item_id, mi.file_path, mi.file_size_bytes, "
        "mi.radarr_id, mi.sonarr_id, mi.season_number, mi.title, mi.plex_rating_key "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.action = 'scheduled_deletion' "
        "  AND sa.execute_at < ? "
        "  AND (sa.delete_status IS NULL OR sa.delete_status = 'pending')",
        (now_iso,),
    ).fetchall()


def mark_delete_status(conn: sqlite3.Connection, action_id: int, status: str) -> None:
    """Set ``scheduled_actions.delete_status`` for the given row id."""
    conn.execute(
        "UPDATE scheduled_actions SET delete_status = ? WHERE id = ?",
        (status, action_id),
    )


def delete_scheduled_action(conn: sqlite3.Connection, action_id: int) -> None:
    """Remove a row from ``scheduled_actions``."""
    conn.execute("DELETE FROM scheduled_actions WHERE id = ?", (action_id,))


def cleanup_expired_snoozes(conn: sqlite3.Connection, now_iso: str) -> None:
    """Remove expired ``snoozed`` rows so items re-enter the scan pipeline."""
    conn.execute(
        "DELETE FROM scheduled_actions WHERE action = 'snoozed' AND execute_at < ?",
        (now_iso,),
    )
