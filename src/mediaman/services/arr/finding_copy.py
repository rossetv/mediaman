"""Whether another copy is looked for of a film or of a season of a show, as ``import_retry`` keeps."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from mediaman.core.time import now_utc, parse_iso_utc
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
    *reasons* will be mended or replaced unasked. A *key* that ends in ":" is any season of a
    series."""
    return bool(fixing_words(conn, key, reasons))


def fixing_words(conn: sqlite3.Connection, key: str, reasons: Sequence[str]) -> str:
    """What mediaman is doing about a download of *key*, as ``import_retry`` keys it, that needs
    attention for *reasons*, in a few words; "" when it leaves it for an admin. A *key* that ends
    in ":" is any season of a series."""
    # Not at the top: import_retry needs the fetcher, which needs import_retry.
    from mediaman.services.arr import import_retry

    if any(not_the_release(r) for r in reasons):
        return ""
    try:
        if not get_bool_setting(conn, import_retry.SETTING, default=True):
            return ""
        # Of a series, the season stuck now, as that one is acted on.
        row = conn.execute(
            "SELECT fix_plan, first_stuck_at FROM arr_import_retries"
            " WHERE key LIKE ? ORDER BY stuck_since IS NULL, first_stuck_at LIMIT 1",
            (f"{key}%" if key.endswith(":") else key,),
        ).fetchone()
        if import_retry.stopped_words(conn, key):
            return ""
    except sqlite3.DatabaseError:
        logger.warning("Failed to read whether %s is tried again", key, exc_info=True)
        return ""
    # As the queue's last fetch planned it; one not noted yet will be replaced.
    plan, first_stuck_at = row if row is not None else (None, None)
    if plan == "":
        return ""
    first = parse_iso_utc(first_stuck_at) if first_stuck_at else None
    if first is not None and (days := (now_utc() - first).days) >= import_retry.LONG_STUCK.days:
        return f"Stuck for {days} days — still trying daily"
    if plan == import_retry.PLAN_RENAME:
        return "Fixing — renaming files"
    if plan == import_retry.PLAN_IMPORT:
        return "Fixing — importing the files"
    return "Fixing — finding another copy"
