"""Date-parsing predicates and guarded UPDATE helpers for ``scheduled_actions``.

``parse_execute_at`` and ``is_pending_unexpired`` are pure-logic helpers
used by both the display layer and the mutation layer.  The two ``apply_*``
functions issue guarded UPDATEs that atomically check action / delete_status /
token_used / execute_at to prevent double-application of a keep decision.

All DB helpers take ``conn: sqlite3.Connection`` and never call
``conn.commit()`` — transaction boundaries belong to the caller.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from mediaman.core.scheduled_action_kinds import (
    ACTION_PROTECTED_FOREVER,
    ACTION_SCHEDULED_DELETION,
    ACTION_SNOOZED,
)
from mediaman.core.time import parse_iso_strict_utc
from mediaman.services.scheduled_actions._types import VerifiedKeepAction

# ---------------------------------------------------------------------------
# Date / duration parsing
# ---------------------------------------------------------------------------


def parse_execute_at(raw: object, *, default: datetime) -> datetime:
    """Parse a stored ``execute_at`` string and return a tz-aware UTC datetime.

    Returns *default* (treat-as-expired) when *raw* is empty,
    unparseable, or otherwise invalid — this is the same fallback the
    keep routes used inline before extraction.

    Delegates to :func:`mediaman.core.time.parse_iso_strict_utc`, which
    preserves the previous inline ``datetime.fromisoformat`` behaviour
    exactly: any value that the old code treated as "unparseable →
    expired" still is.  Naive datetimes are stamped UTC.
    """
    text = str(raw or "")
    parsed = parse_iso_strict_utc(text)
    return parsed if parsed is not None else default


def is_pending_unexpired(verified: VerifiedKeepAction, now: datetime) -> bool:
    """Confirm a :class:`VerifiedKeepAction` is still actionable.

    Returns ``True`` only when the row is a pending
    ``scheduled_deletion`` (delete_status null or "pending") whose
    deadline lies at or after *now*.  Encapsulates the action-state and
    deadline check that was duplicated across the snooze and forever
    POST handlers.
    """
    execute_at = parse_execute_at(verified.execute_at, default=now)
    if execute_at < now:
        return False
    if verified.action != ACTION_SCHEDULED_DELETION:
        return False
    delete_status_val = verified.delete_status
    return not (delete_status_val is not None and delete_status_val != "pending")


# ---------------------------------------------------------------------------
# Mutating helpers — guarded UPDATEs
# ---------------------------------------------------------------------------


def apply_keep_snooze(
    conn: sqlite3.Connection,
    action_id: int,
    duration: str,
    days: int,
    now: datetime,
) -> int:
    """Apply a finite snooze to a ``scheduled_deletion`` row.

    The UPDATE is guarded by ``action='scheduled_deletion'``,
    ``delete_status='pending'``, ``token_used=0`` and
    ``execute_at >= now`` so a concurrent mutation or an already-expired row
    cannot be accidentally applied.  Returns the
    rowcount (0 means nothing happened — caller should respond 409).
    """
    new_execute = (now + timedelta(days=days)).isoformat()
    cursor = conn.execute(
        "UPDATE scheduled_actions SET action=?, token_used=1, "
        "execute_at=?, snoozed_at=?, snooze_duration=? "
        "WHERE id=? AND action='scheduled_deletion' "
        "AND (delete_status IS NULL OR delete_status='pending') "
        "AND token_used=0 AND execute_at >= ?",
        (
            ACTION_SNOOZED,
            new_execute,
            now.isoformat(),
            duration,
            action_id,
            now.isoformat(),
        ),
    )
    return cursor.rowcount


def apply_keep_forever(
    conn: sqlite3.Connection,
    action_id: int,
    now: datetime,
) -> int:
    """Apply a forever-keep to a ``scheduled_deletion`` row.

    Same guards as :func:`apply_keep_snooze`: action, delete_status,
    token_used and execute_at all checked atomically.  Returns the
    rowcount (0 means nothing happened — caller should respond 409).
    """
    cursor = conn.execute(
        "UPDATE scheduled_actions SET action=?, token_used=1, "
        "snoozed_at=?, snooze_duration=? "
        "WHERE id=? AND action='scheduled_deletion' "
        "AND (delete_status IS NULL OR delete_status='pending') "
        "AND token_used=0 AND execute_at >= ?",
        (
            ACTION_PROTECTED_FOREVER,
            now.isoformat(),
            "forever",
            action_id,
            now.isoformat(),
        ),
    )
    return cursor.rowcount
