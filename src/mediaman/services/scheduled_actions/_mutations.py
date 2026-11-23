"""Date-parsing predicates and guarded UPDATE helpers for ``scheduled_actions``.

``parse_execute_at`` and ``is_pending_unexpired`` are pure-logic helpers
used by both the display layer and the mutation layer.  The two ``apply_*``
functions issue guarded UPDATEs that atomically check action / delete_status /
token_used / execute_at to prevent double-application of a keep decision;
``revert_keep_snooze`` undoes a snooze under the same kind of guard.

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


def parse_execute_at(raw: object, *, default: datetime) -> datetime:
    """Parse a stored ``execute_at`` string and return a tz-aware UTC datetime.

    Returns *default* (treat-as-expired) when *raw* is empty,
    unparseable, or otherwise invalid.

    Delegates to ``mediaman.core.time.parse_iso_strict_utc``, so any value
    that is not strict ISO-8601 counts as unparseable → expired.  Naive
    datetimes are stamped UTC.
    """
    text = str(raw or "")
    parsed = parse_iso_strict_utc(text)
    return parsed if parsed is not None else default


def is_pending_unexpired(verified: VerifiedKeepAction, now: datetime) -> bool:
    """Confirm a ``VerifiedKeepAction`` is still actionable.

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
    return delete_status_val in (None, "pending")


def apply_keep_snooze(
    conn: sqlite3.Connection,
    action_id: int,
    duration: str,
    days: int,
    now: datetime,
    *,
    expected_execute_at: str | None = None,
) -> int:
    """Apply a finite snooze to a ``scheduled_deletion`` row.

    The UPDATE is guarded by ``action='scheduled_deletion'``,
    ``delete_status='pending'``, ``token_used=0`` and
    ``execute_at >= now`` so a concurrent mutation or an already-expired row
    cannot be accidentally applied.  With *expected_execute_at* the row must
    also still hold that deletion date, so a caller that read the date to
    restore it later knows it is the one the snooze replaced.  Returns the
    rowcount (0 means nothing happened — caller should respond 409).
    """
    new_execute = (now + timedelta(days=days)).isoformat()
    sql = (
        "UPDATE scheduled_actions SET action=?, token_used=1, "
        "execute_at=?, snoozed_at=?, snooze_duration=? "
        "WHERE id=? AND action='scheduled_deletion' "
        "AND (delete_status IS NULL OR delete_status='pending') "
        "AND token_used=0 AND execute_at >= ?"
    )
    params: list[object] = [
        ACTION_SNOOZED,
        new_execute,
        now.isoformat(),
        duration,
        action_id,
        now.isoformat(),
    ]
    if expected_execute_at is not None:
        sql += " AND execute_at = ?"
        params.append(expected_execute_at)
    return conn.execute(sql, params).rowcount


def revert_keep_snooze(
    conn: sqlite3.Connection,
    action_id: int,
    *,
    kept_until: str,
    execute_at: str,
    snoozed_at: str | None,
    snooze_duration: str | None,
    now: datetime,
) -> int:
    """Undo a snooze written by ``apply_keep_snooze``, putting the deletion back.

    *kept_until* is the ``execute_at`` the snooze wrote; *execute_at*,
    *snoozed_at* and *snooze_duration* are the values it replaced.  The row
    must still be that snooze (``action='snoozed'`` with *kept_until* as its
    date) and its deletion still pending, and the old date must not have
    passed.  ``token_used`` goes back to 0, which is what it was: the snooze
    only applies to a row whose emailed keep link is unused.  Returns the
    rowcount (0 means nothing was undone — caller should respond 409).
    """
    old_date = parse_iso_strict_utc(execute_at)
    if old_date is None or old_date < now:
        return 0
    cursor = conn.execute(
        "UPDATE scheduled_actions SET action=?, token_used=0, "
        "execute_at=?, snoozed_at=?, snooze_duration=? "
        "WHERE id=? AND action=? AND execute_at=? "
        "AND (delete_status IS NULL OR delete_status='pending')",
        (
            ACTION_SCHEDULED_DELETION,
            execute_at,
            snoozed_at,
            snooze_duration,
            action_id,
            ACTION_SNOOZED,
            kept_until,
        ),
    )
    return cursor.rowcount


def apply_keep_forever(
    conn: sqlite3.Connection,
    action_id: int,
    now: datetime,
) -> int:
    """Apply a forever-keep to a ``scheduled_deletion`` row.

    Same guards as ``apply_keep_snooze``: action, delete_status,
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
