"""Delete phase — remove orphaned ``media_items`` rows after a scan.

An orphan is a ``media_items`` row whose ``plex_rating_key`` was not seen
during the most recent Plex fetch for the libraries that were successfully
scanned.  Orphan removal is fail-closed: we refuse to trust a scan that
returns suspiciously few items (a Plex auth hiccup returning zero items
looks identical to a genuine mass-deletion, so we need hard floors).
"""

from __future__ import annotations

import logging
import sqlite3

from mediaman.scanner import repository

logger = logging.getLogger("mediaman")

# Fail-closed safeguard thresholds for orphan detection (C31).
# If the current scan found fewer items than this floor and the previous
# count met it, skip orphan removal (prevents a zero-result scan from
# wiping the DB).
_MIN_ITEMS_TO_TRUST = 5
# Only apply the ratio floor when the previous item count was at least
# this large (avoids false positives on small libraries).
_MIN_ITEMS_FOR_RATIO_CHECK = 50
# Minimum fraction of the previous item count that the current scan must
# return before orphan removal is trusted. A huge drop (e.g. 5 of 200)
# is suspicious.
_MIN_RATIO_TO_TRUST = 0.10


def remove_orphans(
    conn: sqlite3.Connection,
    seen_keys: set[str],
    scanned_libs: set[int],
) -> int:
    """Remove ``media_items`` whose ``plex_rating_key`` is gone from Plex.

    Only considers items belonging to *scanned_libs* (libraries that were
    successfully fetched during this scan run) so items from unreachable
    libraries are never accidentally deleted.

    Args:
        conn: Open SQLite connection.
        seen_keys: Set of ``plex_rating_key`` values observed in the scan.
        scanned_libs: Integer library IDs that were successfully fetched.

    Returns:
        Number of rows deleted.
    """
    if not scanned_libs:
        return 0

    previous_count = repository.count_items_in_libraries(conn, list(scanned_libs))
    current_count = len(seen_keys)

    if current_count < _MIN_ITEMS_TO_TRUST and previous_count >= _MIN_ITEMS_TO_TRUST:
        logger.warning(
            "engine.orphan_guard.skip reason=below_min_items "
            "current=%d previous=%d threshold=%d scanned_libs=%s — "
            "refusing to remove orphans; admin must verify and "
            "reconcile manually if this is correct.",
            current_count,
            previous_count,
            _MIN_ITEMS_TO_TRUST,
            sorted(scanned_libs),
        )
        return 0

    if (
        previous_count > _MIN_ITEMS_FOR_RATIO_CHECK
        and current_count < previous_count * _MIN_RATIO_TO_TRUST
    ):
        logger.warning(
            "engine.orphan_guard.skip reason=below_ratio "
            "current=%d previous=%d ratio=%.3f min_ratio=%.2f "
            "scanned_libs=%s — refusing to remove orphans; admin "
            "must verify and reconcile manually if this is correct.",
            current_count,
            previous_count,
            (current_count / previous_count) if previous_count else 0.0,
            _MIN_RATIO_TO_TRUST,
            sorted(scanned_libs),
        )
        return 0

    all_ids = repository.fetch_ids_in_libraries(conn, list(scanned_libs))
    orphan_ids = [i for i in all_ids if i not in seen_keys]

    if not orphan_ids:
        return 0

    repository.delete_media_items(conn, orphan_ids)
    logger.info(
        "Removed %d orphaned media items no longer in Plex",
        len(orphan_ids),
    )
    return len(orphan_ids)
