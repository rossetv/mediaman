"""Whether another copy is looked for of a film or of a season of a show, as ``import_retry`` keeps."""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


def finding_seasons(conn: sqlite3.Connection, series_id: int) -> set[int]:
    """The numbers of the seasons of Sonarr's series *series_id* whose download was removed and
    another copy of which is looked for."""
    try:
        rows = conn.execute(
            "SELECT key FROM arr_import_retries WHERE key LIKE ? AND retried_at IS NOT NULL",
            (f"sonarr:{series_id}:%",),
        ).fetchall()
    except sqlite3.DatabaseError:
        logger.warning("Failed to read which seasons look for another copy", exc_info=True)
        return set()
    return {int(n) for (key,) in rows if (n := key.rpartition(":")[2]).isdecimal()}


def is_finding_copy(
    conn: sqlite3.Connection, service: str, arr_id: int, season: int | None = None
) -> bool:
    """Whether another copy is looked for of Radarr's film, or of Sonarr's series (of its season
    *season*, else of any)."""
    if service == "sonarr":
        found = finding_seasons(conn, arr_id)
        return bool(found) if season is None else season in found
    try:
        row = conn.execute(
            "SELECT 1 FROM arr_import_retries WHERE key=? AND retried_at IS NOT NULL",
            (f"radarr:{arr_id}",),
        ).fetchone()
    except sqlite3.DatabaseError:
        logger.warning("Failed to read whether a film looks for another copy", exc_info=True)
        return False
    return row is not None
