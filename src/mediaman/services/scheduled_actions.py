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
from datetime import UTC, datetime, timedelta

from mediaman.crypto import validate_keep_token
from mediaman.services.infra.format import format_day_month
from mediaman.web.models import (
    ACTION_PROTECTED_FOREVER,
    ACTION_SCHEDULED_DELETION,
    ACTION_SNOOZED,
)

__all__ = [
    "apply_keep_forever",
    "apply_keep_snooze",
    "find_active_keep_action_by_id_and_token",
    "format_added_display",
    "format_expiry",
    "is_pending_unexpired",
    "lookup_verified_action",
    "mark_token_consumed",
    "parse_execute_at",
    "token_hash",
]


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

    Lookup uses ``token_hash`` first (Finding 16, migration 28 backfills
    existing rows); falls back to raw ``token`` for rows not yet
    migrated so the transition is seamless.
    """
    payload = validate_keep_token(token, secret_key)
    if payload is None:
        return None

    th = token_hash(token)
    row = conn.execute(
        "SELECT sa.*, mi.title, mi.media_type, mi.poster_path, mi.file_size_bytes, "
        "mi.plex_rating_key, mi.added_at, mi.show_title, mi.season_number "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.token_hash = ?",
        (th,),
    ).fetchone()

    # Fall back to raw token column for rows not yet migrated.
    if row is None:
        row = conn.execute(
            "SELECT sa.*, mi.title, mi.media_type, mi.poster_path, mi.file_size_bytes, "
            "mi.plex_rating_key, mi.added_at, mi.show_title, mi.season_number "
            "FROM scheduled_actions sa "
            "JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.token = ?",
            (token,),
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
    now = datetime.now(UTC).isoformat()
    row = conn.execute(
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
    return conn.execute(
        "SELECT * FROM scheduled_actions "
        "WHERE id = ? AND token = ? "
        "AND action = 'scheduled_deletion' "
        "AND (delete_status IS NULL OR delete_status = 'pending') "
        "AND token_used = 0 "
        "AND execute_at >= ?",
        (action_id, token, now),
    ).fetchone()


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

    Uses the strict :func:`datetime.fromisoformat` rather than
    :func:`parse_iso_utc` to preserve the previous inline behaviour
    exactly: any value that the old code treated as "unparseable →
    expired" still is.  Naive datetimes are stamped UTC.
    """
    text = str(raw or "")
    if not text:
        return default
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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

    Uses strict :func:`datetime.fromisoformat` to preserve the prior
    inline behaviour exactly — values that previously fell through to
    ``"Unknown"`` still do.
    """
    if action == ACTION_PROTECTED_FOREVER:
        return "Forever"
    if not execute_at:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(execute_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = (dt - datetime.now(UTC)).days
        if delta <= 0:
            return "Expires today"
        if delta == 1:
            return "Expires tomorrow"
        return f"Expires in {delta} days"
    except (ValueError, TypeError):
        return "Unknown"


def format_added_display(raw_added: object) -> str:
    """Format a stored ``added_at`` value for display on the keep page.

    Renders as ``"5 May 2026"``-style text via the platform-safe
    :func:`format_day_month` helper.  Falls back to the first ten
    characters of the raw string when parsing fails so the template
    still has *something* to render.

    Uses the strict :func:`datetime.fromisoformat` rather than
    :func:`parse_iso_utc` to preserve the previous inline behaviour
    exactly: any value that the old code would have routed to the
    string-slice fallback still does.
    """
    text = str(raw_added or "")
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return text[:10]
    return format_day_month(parsed, long_month=True)
