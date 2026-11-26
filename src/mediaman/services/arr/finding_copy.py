"""Whether another copy is looked for of a film or of a season of a show, as ``import_retry`` keeps."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from mediaman.services.downloads.download_format._classify import not_the_release
from mediaman.services.infra import get_bool_setting

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


def will_retry(conn: sqlite3.Connection, key: str, reasons: Sequence[str]) -> bool:
    """Whether a download of *key*, as ``import_retry`` keys it, that needs attention for
    *reasons* will be removed and another copy looked for unasked. A *key* that ends in ":" is
    any season of a series."""
    # Not at the top: import_retry needs the fetcher, which needs import_retry.
    from mediaman.services.arr import import_retry

    if any(not_the_release(r) for r in reasons):
        return False
    try:
        if not get_bool_setting(conn, import_retry.SETTING, default=True):
            return False
        # Of a series, the fewest tries of a season stuck now, as that one is tried; while none
        # is noted stuck yet, the most of any.
        (tries,) = conn.execute(
            "SELECT COALESCE(MIN(CASE WHEN stuck_since IS NOT NULL THEN tries END), MAX(tries))"
            " FROM arr_import_retries WHERE key LIKE ?",
            (f"{key}%" if key.endswith(":") else key,),
        ).fetchone()
    except sqlite3.DatabaseError:
        logger.warning("Failed to read whether %s is tried again", key, exc_info=True)
        return False
    return (tries or 0) < import_retry.MAX_TRIES
