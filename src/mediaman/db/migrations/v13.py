"""Migration v13: harden ``admin_sessions`` and ``admin_users``.

Adds session-security columns (``token_hash``, ``last_used_at``,
``fingerprint``, ``issued_ip``) to ``admin_sessions`` and a
``must_change_password`` flag to ``admin_users``.

Also purges legacy sessions that lack a ``token_hash`` (unhashed tokens
should not persist) and any sessions whose expiry window exceeded a
reasonable one-day cap, which indicates they were created by an older
implementation with a bug in the expiry calculation.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from mediaman.db.migrations._helpers import _column_exists, _table_exists

logger = logging.getLogger("mediaman")


def apply(conn: sqlite3.Connection) -> None:
    """Add security columns to sessions/users; purge stale legacy sessions."""
    has_sessions = _table_exists(conn, "admin_sessions")
    has_users = _table_exists(conn, "admin_users")

    if has_sessions:
        for col in ("token_hash", "last_used_at", "fingerprint", "issued_ip"):
            if not _column_exists(conn, "admin_sessions", col):
                conn.execute(f"ALTER TABLE admin_sessions ADD COLUMN {col} TEXT")

    if has_users and not _column_exists(conn, "admin_users", "must_change_password"):
        conn.execute(
            "ALTER TABLE admin_users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
        )

    if has_sessions:
        deleted_null = conn.execute(
            "DELETE FROM admin_sessions WHERE token_hash IS NULL OR token_hash = ''"
        ).rowcount
        if deleted_null:
            logger.warning(
                "db.migration_v13 purged_legacy_sessions count=%d reason=token_hash_missing",
                deleted_null,
            )
        cap = timedelta(days=1, seconds=60)
        _rows = conn.execute(
            "SELECT rowid AS rid, created_at, expires_at FROM admin_sessions "
            "WHERE created_at IS NOT NULL AND expires_at IS NOT NULL"
        ).fetchall()
        stale_rowids: list[int] = []
        for _row in _rows:
            try:
                created = datetime.fromisoformat(_row[1])
                expires = datetime.fromisoformat(_row[2])
            except (TypeError, ValueError):
                continue
            if expires - created > cap:
                stale_rowids.append(_row[0])
        if stale_rowids:
            placeholders = ",".join("?" for _ in stale_rowids)
            conn.execute(
                f"DELETE FROM admin_sessions WHERE rowid IN ({placeholders})",
                stale_rowids,
            )
            logger.warning(
                "db.migration_v13 purged_legacy_sessions count=%d reason=expiry_over_cap",
                len(stale_rowids),
            )
