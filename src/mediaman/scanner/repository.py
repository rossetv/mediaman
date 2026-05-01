"""SQL repository for the scanner.

Every ``conn.execute(...)`` that talks to ``media_items``,
``scheduled_actions``, ``audit_log``, ``kept_shows`` or ``snoozes`` on
behalf of the scanner lives here. Keeping SQL in one module means the
engine, fetcher, and deletion executor read as orchestration — not as
a pile of string literals — and makes the schema contract easy to spot
when it changes.

This module depends only on :mod:`sqlite3`; it MUST NOT import from
``fetch`` or ``deletions`` (see engine.py header for the import-cycle
rule).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from mediaman.audit import log_audit
from mediaman.crypto import generate_keep_token
from mediaman.services.infra.format import ensure_tz as _ensure_tz
from mediaman.services.infra.format import parse_iso_utc as _parse_iso_utc
from mediaman.services.infra.time import now_iso

logger = logging.getLogger("mediaman")

# The action that means deletion is already lined up.
DELETION_ACTION = "scheduled_deletion"

# Default token TTL: 30 days from now.
_TOKEN_TTL_DAYS = 30


# ---------------------------------------------------------------------------
# media_items
# ---------------------------------------------------------------------------


def upsert_media_item(
    conn: sqlite3.Connection,
    *,
    item: dict,
    library_id: str,
    media_type: str,
    arr_date: str | None,
) -> None:
    """Insert or update a media item record.

    Uses *arr_date* (from Radarr/Sonarr) when available, else falls back
    to Plex's ``addedAt``. The ``added_at`` column is always updated to
    reflect the best known date.
    """
    now = now_iso()

    if arr_date:
        parsed = _parse_iso_utc(arr_date)
        added_at = parsed.isoformat() if parsed else arr_date
    else:
        added_at = item.get("added_at")
        if isinstance(added_at, datetime):
            added_at = _ensure_tz(added_at).isoformat()
        elif added_at is None:
            added_at = now

    conn.execute(
        """
        INSERT INTO media_items (
            id, title, media_type, show_title, season_number,
            plex_library_id, plex_rating_key, show_rating_key,
            added_at, file_path, file_size_bytes, poster_path, last_scanned_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            media_type = excluded.media_type,
            show_rating_key = excluded.show_rating_key,
            added_at = excluded.added_at,
            file_path = excluded.file_path,
            file_size_bytes = excluded.file_size_bytes,
            poster_path = excluded.poster_path,
            last_scanned_at = excluded.last_scanned_at
        """,
        (
            item["plex_rating_key"],
            item["title"],
            media_type,
            item.get("show_title"),
            item.get("season_number"),
            int(library_id) if str(library_id).isdigit() else library_id,
            item["plex_rating_key"],
            item.get("show_rating_key"),
            added_at,
            item.get("file_path", ""),
            item.get("file_size_bytes", 0),
            item.get("poster_path"),
            now,
        ),
    )


def update_last_watched(conn: sqlite3.Connection, media_id: str, watch_history: list[dict]) -> None:
    """Store the most recent watch timestamp for a media item."""
    if not watch_history:
        return
    latest = max(
        (h["viewed_at"] for h in watch_history if h.get("viewed_at")),
        default=None,
    )
    if latest is None:
        return
    latest = _ensure_tz(latest)
    conn.execute(
        "UPDATE media_items SET last_watched_at = ? WHERE id = ?",
        (latest.isoformat(), media_id),
    )


def count_items_in_libraries(conn: sqlite3.Connection, library_ids: list[int]) -> int:
    """Return the total number of ``media_items`` in *library_ids*."""
    if not library_ids:
        return 0
    lp = ",".join("?" * len(library_ids))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM media_items WHERE plex_library_id IN ({lp})",  # noqa: S608 — placeholders are '?' only, not user input
        tuple(library_ids),
    ).fetchone()
    return row["n"] if row else 0


def fetch_ids_in_libraries(conn: sqlite3.Connection, library_ids: list[int]) -> list[str]:
    """Return every ``media_items.id`` belonging to *library_ids*.

    Chunks into groups of 500 to stay below SQLite's parameter limit.
    """
    ids: list[str] = []
    for start in range(0, len(library_ids), 500):
        chunk = library_ids[start : start + 500]
        lp = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id FROM media_items WHERE plex_library_id IN ({lp})",  # noqa: S608
            tuple(chunk),
        ).fetchall()
        ids.extend(r["id"] for r in rows)
    return ids


def delete_media_items(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Delete ``media_items`` rows and their ``scheduled_actions`` in chunks."""
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM scheduled_actions WHERE media_item_id IN ({placeholders})",  # noqa: S608 — placeholders are '?' only, not user input
            tuple(chunk),
        )
        conn.execute(
            f"DELETE FROM media_items WHERE id IN ({placeholders})",  # noqa: S608 — placeholders are '?' only, not user input
            tuple(chunk),
        )


# ---------------------------------------------------------------------------
# scheduled_actions — protection / schedule queries
# ---------------------------------------------------------------------------


def is_protected(conn: sqlite3.Connection, media_id: str) -> bool:
    """Return True if the item has an active protection action.

    An item is protected if it has a ``protected_forever`` action
    (regardless of ``token_used``) or a ``snoozed`` action whose
    ``execute_at`` is still in the future.
    """
    now = now_iso()
    row = conn.execute(
        """
        SELECT action, execute_at FROM scheduled_actions
        WHERE media_item_id = ?
          AND action IN ('protected_forever', 'snoozed')
        ORDER BY id DESC LIMIT 1
        """,
        (media_id,),
    ).fetchone()
    if row is None:
        return False
    if row["action"] == "protected_forever":
        return True
    # Snoozed — only protected if execute_at is in the future.
    return row["execute_at"] is not None and row["execute_at"] > now


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


def is_show_kept(conn: sqlite3.Connection, show_rating_key: str | None) -> bool:
    """Return True if the show has an active keep rule in ``kept_shows``.

    Side effect: deletes expired snooze rows from ``kept_shows`` when a
    snoozed entry is found past its ``execute_at`` timestamp.
    """
    if not show_rating_key:
        return False
    now = now_iso()
    row = conn.execute(
        """
        SELECT id, action, execute_at FROM kept_shows
        WHERE show_rating_key = ?
        LIMIT 1
        """,
        (show_rating_key,),
    ).fetchone()
    if row is None:
        return False
    if row["action"] == "protected_forever":
        return True
    if row["execute_at"] and row["execute_at"] > now:
        return True
    # Expired snooze — clean up.
    conn.execute("DELETE FROM kept_shows WHERE id = ?", (row["id"],))
    return False


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
) -> None:
    """Insert a scheduled_deletion row and write an audit entry.

    Uses a unique random placeholder token for the initial insert so
    the ``token`` unique index can't collide between concurrent scheduler
    runs, then swaps in the real HMAC-signed keep token once we know the
    row id.
    """
    now = datetime.now(timezone.utc)
    execute_at = now + timedelta(days=grace_days)
    expires_at = int((now + timedelta(days=_TOKEN_TTL_DAYS)).timestamp())

    # Finding 16: use a placeholder for the initial insert (satisfies
    # any remaining NOT NULL constraint on legacy schemas before migration 28).
    # After migration 28 the token column is nullable so this placeholder
    # is only needed as a uniqueness sentinel.
    placeholder = f"pending-{secrets.token_urlsafe(16)}"

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
    action_id = cursor.lastrowid

    token = generate_keep_token(
        media_item_id=media_id,
        action_id=action_id,
        expires_at=expires_at,
        secret_key=secret_key,
    )
    import hashlib as _hashlib

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


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def read_delete_allowed_roots_setting(
    conn: sqlite3.Connection,
) -> list[str]:
    """Read ``delete_allowed_roots`` from settings / env.

    Returns an empty list if nothing is configured; the caller must
    treat an empty list as fail-closed.
    """
    row = conn.execute("SELECT value FROM settings WHERE key='delete_allowed_roots'").fetchone()
    roots: list[str] = []
    if row and row["value"]:
        try:
            parsed = json.loads(row["value"])
            if isinstance(parsed, list):
                roots = [str(r) for r in parsed if r]
        except (ValueError, TypeError):
            pass
    if not roots:
        env_val = os.environ.get("MEDIAMAN_DELETE_ROOTS", "")
        if env_val:
            # Single source of truth lives in path_safety.parse_delete_roots_env
            # so the deletion path and the disk-usage path always agree on
            # separator handling (finding 31).
            from mediaman.services.infra.path_safety import parse_delete_roots_env

            roots = parse_delete_roots_env(env_val)
            if not roots:
                logger.error(
                    "MEDIAMAN_DELETE_ROOTS is set but no valid roots "
                    "parsed from %r — deletions will be refused.",
                    env_val,
                )
    if not roots:
        logger.error(
            "delete_allowed_roots is not configured — all deletions "
            "will be refused. Set the delete_allowed_roots setting "
            "(JSON list) or the MEDIAMAN_DELETE_ROOTS env var "
            "(colon-separated) to re-enable deletions."
        )
    return roots
