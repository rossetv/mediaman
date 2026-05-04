"""Migration v11: add ``tvdb_id`` to ``download_notifications``; migrate Sonarr rows.

Sonarr uses TVDB IDs, not TMDB IDs.  The original schema stored both kinds
in ``tmdb_id``, making Sonarr look-ups unreliable.  This migration adds a
dedicated ``tvdb_id`` column and moves the TVDB values out of ``tmdb_id``
for any existing Sonarr rows.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add ``tvdb_id`` column and backfill Sonarr rows from ``tmdb_id``."""
    if not _column_exists(conn, "download_notifications", "tvdb_id"):
        conn.execute("ALTER TABLE download_notifications ADD COLUMN tvdb_id INTEGER")
    conn.execute(
        "UPDATE download_notifications "
        "SET tvdb_id = tmdb_id, tmdb_id = NULL "
        "WHERE service = 'sonarr' AND tvdb_id IS NULL"
    )
