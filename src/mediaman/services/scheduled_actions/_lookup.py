"""Token helpers and DB-lookup functions for ``scheduled_actions``.

Contains the SHA-256 token-hash primitive, the ``sqlite3.Row`` → dataclass
mapper, the two keep-action lookup queries, and the token-consumed insert.
All DB helpers take ``conn: sqlite3.Connection`` and never call
``conn.commit()`` — transaction boundaries belong to the caller.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

from mediaman.crypto import validate_keep_token
from mediaman.services.scheduled_actions._types import VerifiedKeepAction


def token_hash(token: str) -> str:
    """Return a hex SHA-256 digest of *token* for storage in ``keep_tokens_used``.

    The raw token is hashed before storage so a leaked DB dump cannot replay
    snooze actions.  Lookups must hash the inbound token before comparing.
    """
    return hashlib.sha256(token.encode()).hexdigest()


# SELECT column list shared by both keep-action lookups: every
# ``scheduled_actions`` column followed by the ``media_items`` display
# columns.  Both queries below inline exactly these columns in this
# order so a single ``_row_to_verified_keep_action`` mapper covers both.
# It is kept as a literal in each query (not interpolated) so no string
# is ever built into SQL — see CODE_GUIDELINES §9.6.


def _row_to_verified_keep_action(row: sqlite3.Row) -> VerifiedKeepAction:
    """Map a joined ``scheduled_actions`` + ``media_items`` row to :class:`VerifiedKeepAction`.

    *row* must have been produced by one of the two lookup queries in
    this module, which project every ``scheduled_actions`` column plus
    the ``media_items`` display columns.  Access is by column name so the
    mapper is insensitive to any future re-ordering of the SELECT list.
    """
    return VerifiedKeepAction(
        id=row["id"],
        media_item_id=row["media_item_id"],
        action=row["action"],
        scheduled_at=row["scheduled_at"],
        execute_at=row["execute_at"],
        token=row["token"],
        token_used=row["token_used"],
        snoozed_at=row["snoozed_at"],
        snooze_duration=row["snooze_duration"],
        notified=row["notified"],
        is_reentry=row["is_reentry"],
        delete_status=row["delete_status"],
        token_hash=row["token_hash"],
        title=row["title"],
        media_type=row["media_type"],
        poster_path=row["poster_path"],
        file_size_bytes=row["file_size_bytes"],
        plex_rating_key=row["plex_rating_key"],
        added_at=row["added_at"],
        show_title=row["show_title"],
        season_number=row["season_number"],
    )


def lookup_verified_action(
    conn: sqlite3.Connection, token: str, secret_key: str
) -> VerifiedKeepAction | None:
    """Validate the keep-token HMAC, then look up its ``scheduled_actions`` row.

    Returns the row joined with ``media_items`` (so the caller has the
    title, poster, etc. for display) as a :class:`VerifiedKeepAction`, or
    ``None`` for any failure: bad signature, expired token, token/payload
    mismatch, or row absent.  Rejecting on signature first stops forged
    tokens reaching the DB lookup at all.

    Lookup is by ``token_hash`` so the raw token never lands in the index.
    """
    payload = validate_keep_token(token, secret_key)
    if payload is None:
        return None

    row: sqlite3.Row | None = conn.execute(
        "SELECT sa.id, sa.media_item_id, sa.action, sa.scheduled_at, sa.execute_at, "
        "sa.token, sa.token_used, sa.snoozed_at, sa.snooze_duration, sa.notified, "
        "sa.is_reentry, sa.delete_status, sa.token_hash, "
        "mi.title, mi.media_type, mi.poster_path, mi.file_size_bytes, "
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

    return _row_to_verified_keep_action(row)


def find_active_keep_action_by_id_and_token(
    conn: sqlite3.Connection, action_id: int, token: str
) -> VerifiedKeepAction | None:
    """Look up an active ``scheduled_deletion`` row by ``action_id`` + token hash.

    Returns a :class:`VerifiedKeepAction` when ``action='scheduled_deletion'``,
    ``delete_status='pending'``, ``token_used=0`` and the deadline has
    not yet passed; otherwise ``None``.  Falls back to the raw token
    column for rows not yet migrated to ``token_hash``.

    The ``JOIN media_items`` is on the ``media_item_id`` foreign key,
    which is ``NOT NULL`` and always references an existing row, so the
    join cannot change which ``scheduled_actions`` rows match — it only
    attaches the display columns needed for the shared return shape.
    """
    from mediaman.core.time import now_iso

    th = token_hash(token)
    now = now_iso()
    row: sqlite3.Row | None = conn.execute(
        "SELECT sa.id, sa.media_item_id, sa.action, sa.scheduled_at, sa.execute_at, "
        "sa.token, sa.token_used, sa.snoozed_at, sa.snooze_duration, sa.notified, "
        "sa.is_reentry, sa.delete_status, sa.token_hash, "
        "mi.title, mi.media_type, mi.poster_path, mi.file_size_bytes, "
        "mi.plex_rating_key, mi.added_at, mi.show_title, mi.season_number "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.id = ? AND sa.token_hash = ? "
        "AND sa.action = 'scheduled_deletion' "
        "AND (sa.delete_status IS NULL OR sa.delete_status = 'pending') "
        "AND sa.token_used = 0 "
        "AND sa.execute_at >= ?",
        (action_id, th, now),
    ).fetchone()
    if row is not None:
        return _row_to_verified_keep_action(row)
    result: sqlite3.Row | None = conn.execute(
        "SELECT sa.id, sa.media_item_id, sa.action, sa.scheduled_at, sa.execute_at, "
        "sa.token, sa.token_used, sa.snoozed_at, sa.snooze_duration, sa.notified, "
        "sa.is_reentry, sa.delete_status, sa.token_hash, "
        "mi.title, mi.media_type, mi.poster_path, mi.file_size_bytes, "
        "mi.plex_rating_key, mi.added_at, mi.show_title, mi.season_number "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.id = ? AND sa.token = ? "
        "AND sa.action = 'scheduled_deletion' "
        "AND (sa.delete_status IS NULL OR sa.delete_status = 'pending') "
        "AND sa.token_used = 0 "
        "AND sa.execute_at >= ?",
        (action_id, token, now),
    ).fetchone()
    if result is None:
        return None
    return _row_to_verified_keep_action(result)


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
