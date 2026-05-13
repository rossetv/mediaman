"""Shared service helpers for the ``scheduled_actions`` table.

Domain 02 noted that ``web/routes/keep.py`` and ``web/routes/kept.py`` had
extensive overlap: the same execute-at parsing, the same token-hash insert
into ``keep_tokens_used``, the same guarded UPDATE for snooze/forever, and
the same human-readable expiry formatter were copy-pasted across both
files (and twice within ``keep.py`` alone).  This module is the single
source of truth for that logic so the route layer stays thin.

All DB-bound helpers take ``conn: sqlite3.Connection`` as the first
positional argument and never call ``conn.commit()`` themselves —
commits and rollbacks remain the route's responsibility so a single
HTTP request still maps to a single transaction boundary.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from mediaman.core.format import format_day_month, relative_day_label
from mediaman.core.scheduled_action_kinds import (
    ACTION_PROTECTED_FOREVER,
    ACTION_SCHEDULED_DELETION,
    ACTION_SNOOZED,
)
from mediaman.core.time import now_iso, now_utc, parse_iso_strict_utc
from mediaman.crypto import validate_keep_token

__all__ = [
    "KeepDecision",
    "apply_keep_forever",
    "apply_keep_snooze",
    "find_active_keep_action_by_id_and_token",
    "format_added_display",
    "format_expiry",
    "is_pending_unexpired",
    "lookup_verified_action",
    "mark_token_consumed",
    "parse_execute_at",
    "resolve_keep_decision",
    "token_hash",
]


@dataclass(frozen=True, slots=True)
class KeepDecision:
    """Resolved outcome of a keep-duration choice.

    ``action`` is one of :data:`ACTION_PROTECTED_FOREVER` /
    :data:`ACTION_SNOOZED`.  ``execute_at`` is the ISO UTC deadline for
    a finite snooze, ``None`` for forever.  ``snooze_duration_days`` is
    the integer day count for a finite snooze, ``None`` for forever.
    """

    action: str
    execute_at: str | None
    snooze_duration_days: int | None


def resolve_keep_decision(duration: str, *, days: int | None, now: datetime) -> KeepDecision:
    """Resolve the keep-duration ladder once.

    Two route handlers previously duplicated this if/else chain:
    ``web/routes/library_api/__init__.py`` (``api_media_keep``) and
    ``web/routes/kept.py`` (``api_keep_show``).  ``duration`` must be a
    value already validated against :data:`VALID_KEEP_DURATIONS`;
    ``days`` is the integer day count from the same lookup
    (``VALID_KEEP_DURATIONS[duration]``) — ``None`` only when
    ``duration == "forever"``.  Passing them separately rather than
    importing :data:`VALID_KEEP_DURATIONS` here keeps Ring-2 services
    free of any Ring-3 ``web.models`` dependency.
    """
    if duration == "forever":
        return KeepDecision(ACTION_PROTECTED_FOREVER, None, None)
    assert days is not None, "non-forever durations must have a day count"
    execute_at = (now + timedelta(days=days)).isoformat()
    return KeepDecision(ACTION_SNOOZED, execute_at, days)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def token_hash(token: str) -> str:
    """Return a hex SHA-256 digest of *token* for storage in ``keep_tokens_used``.

    Storing a hash rather than the raw token (Finding 16) means a leaked
    DB dump cannot replay snooze actions.  Lookups must hash the inbound
    token before comparing.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def lookup_verified_action(
    conn: sqlite3.Connection, token: str, secret_key: str
) -> sqlite3.Row | None:
    """Validate the keep-token HMAC, then look up its ``scheduled_actions`` row.

    Returns the row joined with ``media_items`` (so the caller has the
    title, poster, etc. for display) or ``None`` for any failure: bad
    signature, expired token, token/payload mismatch, or row absent.
    Rejecting on signature first stops forged tokens reaching the DB
    lookup at all.

    Lookup is by ``token_hash`` (Finding 16): the raw token never lands
    in the index.
    """
    payload = validate_keep_token(token, secret_key)
    if payload is None:
        return None

    row: sqlite3.Row | None = conn.execute(
        "SELECT sa.*, mi.title, mi.media_type, mi.poster_path, mi.file_size_bytes, "
        "mi.plex_rating_key, mi.added_at, mi.show_title, mi.season_number "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.token_hash = ?",
        (token_hash(token),),
    ).fetchone()

    if row is None:
        return None

    # The signed payload must reference the same scheduled action as the
    # DB row.  Reject any mismatch — a token that validates but points
    # at a different action is tampered and must not be honoured.
    if str(payload.get("media_item_id")) != str(row["media_item_id"]) or int(
        payload.get("action_id", -1)
    ) != int(row["id"]):
        return None

    return row


def find_active_keep_action_by_id_and_token(
    conn: sqlite3.Connection, action_id: int, token: str
) -> sqlite3.Row | None:
    """Look up an active ``scheduled_deletion`` row by ``action_id`` + token hash.

    Returns the row when ``action='scheduled_deletion'``,
    ``delete_status='pending'``, ``token_used=0`` and the deadline has
    not yet passed; otherwise ``None``.  Falls back to the raw token
    column for rows not yet migrated to ``token_hash``.
    """
    th = token_hash(token)
    now = now_iso()
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM scheduled_actions "
        "WHERE id = ? AND token_hash = ? "
        "AND action = 'scheduled_deletion' "
        "AND (delete_status IS NULL OR delete_status = 'pending') "
        "AND token_used = 0 "
        "AND execute_at >= ?",
        (action_id, th, now),
    ).fetchone()
    if row is not None:
        return row
    result: sqlite3.Row | None = conn.execute(
        "SELECT * FROM scheduled_actions "
        "WHERE id = ? AND token = ? "
        "AND action = 'scheduled_deletion' "
        "AND (delete_status IS NULL OR delete_status = 'pending') "
        "AND token_used = 0 "
        "AND execute_at >= ?",
        (action_id, token, now),
    ).fetchone()
    return result


def mark_token_consumed(conn: sqlite3.Connection, token: str, now: datetime) -> bool:
    """Insert a token hash into ``keep_tokens_used``; return ``True`` on a fresh insert.

    Uses ``INSERT OR IGNORE`` so a replay returns ``rowcount == 0`` →
    ``False``.  The caller is responsible for committing the
    transaction (success or replay) and for translating ``False`` into
    a 409 response.
    """
    cursor = conn.execute(
        "INSERT OR IGNORE INTO keep_tokens_used (token_hash, used_at) VALUES (?, ?)",
        (token_hash(token), now.isoformat()),
    )
    return cursor.rowcount > 0


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


def is_pending_unexpired(verified: sqlite3.Row, now: datetime) -> bool:
    """Confirm a ``scheduled_actions`` row is still actionable.

    Returns ``True`` only when the row is a pending
    ``scheduled_deletion`` (delete_status null or "pending") whose
    deadline lies at or after *now*.  Encapsulates the action-state and
    deadline check that was duplicated across the snooze and forever
    POST handlers.
    """
    execute_at = parse_execute_at(verified["execute_at"], default=now)
    if execute_at < now:
        return False
    keys = verified.keys()
    action_val = verified["action"] if "action" in keys else ""
    delete_status_val = verified["delete_status"] if "delete_status" in keys else "pending"
    if action_val != ACTION_SCHEDULED_DELETION:
        return False
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
    ``execute_at >= now`` (Finding 13) so a concurrent mutation or an
    already-expired row cannot accidentally be applied.  Returns the
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


# ---------------------------------------------------------------------------
# Display formatters
# ---------------------------------------------------------------------------


def format_expiry(action: str | None, execute_at: str | None) -> str:
    """Return a human-readable expiry string for a protected item.

    * ``"Forever"`` for ``protected_forever``.
    * ``"Expires today"`` / ``"Expires tomorrow"`` / ``"Expires in N days"``
      for snoozed items with a parseable future deadline.
    * ``"Unknown"`` for missing or unparseable deadlines.
    """
    if action == ACTION_PROTECTED_FOREVER:
        return "Forever"
    dt = parse_iso_strict_utc(execute_at)
    if dt is None:
        return "Unknown"
    return relative_day_label(
        dt,
        now=now_utc(),
        today="Expires today",
        tomorrow="Expires tomorrow",
        future=lambda days: f"Expires in {days} days",
    )


def format_added_display(raw_added: object) -> str:
    """Format a stored ``added_at`` value for display on the keep page.

    Renders as ``"5 May 2026"``-style text via the platform-safe
    :func:`format_day_month` helper.  Falls back to the first ten
    characters of the raw string when parsing fails so the template
    still has *something* to render.

    Delegates to :func:`mediaman.core.time.parse_iso_strict_utc`, which
    preserves the previous inline ``datetime.fromisoformat`` behaviour
    exactly: any value that the old code would have routed to the
    string-slice fallback still does.
    """
    text = str(raw_added or "")
    if not text:
        return ""
    parsed = parse_iso_strict_utc(text)
    if parsed is None:
        return text[:10]
    return format_day_month(parsed, long_month=True)
