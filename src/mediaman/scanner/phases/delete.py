"""Delete phase — remove orphaned ``media_items`` rows after a scan.

An orphan is a ``media_items`` row whose ``plex_rating_key`` was not seen
during the most recent Plex fetch for the libraries that were successfully
scanned.  A suspiciously large drop (a Plex auth hiccup returning zero items
looks identical to a genuine mass-deletion on a single scan) is not acted on
immediately: the first such scan marks the library *pending* and skips, and
only a second consecutive suspicious scan confirms the drop and prunes. A
one-off glitch recovers before the second scan; a real deletion persists and
reconciles. See :func:`remove_orphans`.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from mediaman.core.time import now_iso
from mediaman.scanner import repository

logger = logging.getLogger(__name__)

# Fail-closed safeguard thresholds for orphan detection (C31).
# If the current scan found fewer items than this floor and the previous
# count met it, treat the scan as suspicious (prevents a zero-result scan
# from wiping the DB on a transient Plex hiccup).
_MIN_ITEMS_TO_TRUST = 5
# Only apply the ratio floor when the previous item count was at least
# this large (avoids false positives on small libraries).
_MIN_ITEMS_FOR_RATIO_CHECK = 50
# Minimum fraction of the previous item count that the current scan must
# return before orphan removal is trusted. A huge drop (e.g. 5 of 200)
# is suspicious.
_MIN_RATIO_TO_TRUST = 0.10

# Settings key holding the set of library ids whose previous scan tripped
# the suspicious-drop guard and are awaiting a second confirming scan.
_PENDING_GUARD_KEY = "orphan_guard_pending"


def _suspicious_reason(current_count: int, previous_count: int) -> str | None:
    """Return a guard-trip reason for a suspicious item-count drop, else None."""
    if current_count < _MIN_ITEMS_TO_TRUST and previous_count >= _MIN_ITEMS_TO_TRUST:
        return "below_min_items"
    if (
        previous_count > _MIN_ITEMS_FOR_RATIO_CHECK
        and current_count < previous_count * _MIN_RATIO_TO_TRUST
    ):
        return "below_ratio"
    return None


def _get_pending_guard_libs(conn: sqlite3.Connection) -> set[int]:
    """Return the set of library ids awaiting a confirming second scan."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (_PENDING_GUARD_KEY,)).fetchone()
    if not row:
        return set()
    try:
        data = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {int(x) for x in data}


def _set_pending_guard_libs(conn: sqlite3.Connection, libs: set[int]) -> None:
    """Persist the set of library ids awaiting a confirming second scan."""
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (_PENDING_GUARD_KEY, json.dumps(sorted(libs)), now_iso()),
    )


def remove_orphans(
    conn: sqlite3.Connection,
    seen_keys: set[str],
    scanned_libs: set[int],
) -> int:
    """Remove ``media_items`` whose ``plex_rating_key`` is gone from Plex.

    Only considers items belonging to *scanned_libs* (libraries that were
    successfully fetched during this scan run) so items from unreachable
    libraries are never accidentally deleted.

    A suspicious item-count drop (see :func:`_suspicious_reason`) — which a
    transient Plex hiccup and a genuine mass-deletion look identical on a
    single scan — is no longer refused outright. The first suspicious scan
    for a library records it as *pending* and skips; a second consecutive
    suspicious scan confirms the drop is real (a transient empty would have
    recovered by then) and proceeds with removal. This lets a legitimately
    shrunk library (e.g. the last show deleted) reconcile on the next scan
    instead of sticking forever, while still absorbing a one-off glitch.
    Pruning only removes mediaman's tracking rows — never media files — and a
    later healthy scan re-populates anything Plex still has, so the cost of a
    wrong prune is bounded.

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

    reason = _suspicious_reason(current_count, previous_count)
    pending = _get_pending_guard_libs(conn)

    if reason is not None and not scanned_libs <= pending:
        # First suspicious scan for at least one of these libraries — record
        # and wait for a second scan to confirm before touching anything.
        _set_pending_guard_libs(conn, pending | scanned_libs)
        conn.commit()
        logger.warning(
            "engine.orphan_guard.skip reason=%s current=%d previous=%d "
            "scanned_libs=%s — suspicious drop; awaiting a confirming second "
            "scan before removing orphans.",
            reason,
            current_count,
            previous_count,
            sorted(scanned_libs),
        )
        return 0

    if reason is not None:
        logger.warning(
            "engine.orphan_guard.confirm reason=%s current=%d previous=%d "
            "scanned_libs=%s — low item count seen on two consecutive scans; "
            "treating as a genuine deletion and removing orphans.",
            reason,
            current_count,
            previous_count,
            sorted(scanned_libs),
        )

    # Either a healthy scan or a confirmed drop — clear any pending marker
    # for these libraries so a future glitch starts the two-scan cycle anew.
    if pending & scanned_libs:
        _set_pending_guard_libs(conn, pending - scanned_libs)

    all_ids = repository.fetch_ids_in_libraries(conn, list(scanned_libs))
    orphan_ids = [i for i in all_ids if i not in seen_keys]

    if not orphan_ids:
        return 0

    # Atomic two-table delete: the matching ``scheduled_actions`` rows
    # are dropped first, then the ``media_items`` rows, both inside one
    # transaction so a crash, foreign-key violation, or concurrent
    # writer cannot leave a ``scheduled_actions`` row pointing at a
    # deleted ``media_items`` row. Each repository call chunks its own
    # IN-clause; the ``with conn:`` here owns the transaction boundary.
    with conn:
        repository.delete_actions_for_media_items(conn, orphan_ids)
        repository.delete_media_items(conn, orphan_ids)
    logger.info(
        "Removed %d orphaned media items no longer in Plex",
        len(orphan_ids),
    )
    return len(orphan_ids)
