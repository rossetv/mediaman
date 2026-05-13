"""Upsert phase — write fetched Plex items into ``media_items``.

Owns all DB mutation that occurs during the per-item scan loop:

1. ``upsert_media_item`` — insert or update the ``media_items`` row.
2. ``update_last_watched`` — advance the ``last_watched_at`` column.
3. ``schedule_item_deletion`` — insert a ``scheduled_actions`` row for
   items that are eligible for deletion, including HMAC token generation.

Token generation lives here (not in :mod:`mediaman.scanner.repository`)
so the repository remains a pure SQL layer.  The token requires the
``action_id`` returned by the initial INSERT, which is why the two-step
insert → hash → update lives in this module rather than being split
across the call-stack.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from datetime import timedelta

from mediaman.core.audit import log_audit
from mediaman.core.time import now_utc
from mediaman.crypto import generate_keep_token
from mediaman.scanner import repository
from mediaman.scanner.arr_dates import ArrDateCache
from mediaman.scanner.fetch import _PlexItemFetch

logger = logging.getLogger(__name__)

# Default keep-token TTL: 30 days from the scheduling moment.
_TOKEN_TTL_DAYS = 30

# The action string that represents a pending deletion in scheduled_actions.
DELETION_ACTION = "scheduled_deletion"


def upsert_item(
    conn: sqlite3.Connection,
    fetch: _PlexItemFetch,
    arr_cache: ArrDateCache,
    media_type: str,
) -> None:
    """Upsert a single media item and advance its watch timestamp.

    Loads the Arr date cache on demand (idempotent) then delegates to
    :func:`repository.upsert_media_item` and
    :func:`repository.update_last_watched`.

    Args:
        conn: Open SQLite connection.
        fetch: The network-layer record to persist.
        arr_cache: Radarr/Sonarr date cache used to resolve ``added_at``.
        media_type: Canonical media-type string (``"movie"`` or ``"season"``).
    """
    arr_cache.ensure_loaded()
    file_path = fetch.item.get("file_path") or ""
    if not isinstance(file_path, str):
        file_path = ""
    arr_date = arr_cache.get(file_path)
    repository.upsert_media_item(
        conn,
        item=fetch.item,
        library_id=fetch.library_id,
        media_type=media_type,
        arr_date=arr_date,
    )
    repository.update_last_watched(conn, fetch.item["plex_rating_key"], fetch.watch_history)


def schedule_deletion(
    conn: sqlite3.Connection,
    *,
    media_id: str,
    is_reentry: bool,
    grace_days: int,
    secret_key: str,
) -> str:
    """Insert a ``scheduled_deletion`` row and write an audit entry.

    Token generation lives here rather than in :mod:`repository` so the
    repository remains a pure SQL layer.  The HMAC keep-token is bound to
    the ``action_id`` assigned by SQLite, so we must:

    1. INSERT the row with a random placeholder token to satisfy any
       ``NOT NULL`` constraint and the unique index.
    2. Read back the ``lastrowid``.
    3. Generate the real token using ``action_id``.
    4. UPDATE the row to store only the SHA-256 hash of the token,
       nulling out the raw value.

    Returns:
        ``"scheduled"`` on success, ``"skipped"`` if a concurrent scan has
        already inserted an active deletion for the same ``media_id``
        (the partial unique index raises ``IntegrityError`` in that case).
    """
    now = now_utc()
    execute_at = now + timedelta(days=grace_days)
    expires_at = int((now + timedelta(days=_TOKEN_TTL_DAYS)).timestamp())

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
        logger.info(
            "upsert.schedule_deletion.skip media_id=%s reason=integrity_error",
            media_id,
        )
        return "skipped"

    action_id = cursor.lastrowid
    assert action_id is not None  # always populated after a successful INSERT

    token = generate_keep_token(
        media_item_id=media_id,
        action_id=action_id,
        expires_at=expires_at,
        secret_key=secret_key,
    )
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    conn.execute(
        "UPDATE scheduled_actions SET token_hash = ?, token = NULL WHERE id = ?",
        (token_hash, action_id),
    )

    log_audit(
        conn,
        media_id,
        DELETION_ACTION,
        "scheduled by scan engine" + (" (re-entry)" if is_reentry else ""),
    )
    return "scheduled"
