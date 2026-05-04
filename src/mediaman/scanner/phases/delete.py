"""Delete phase — remove orphaned ``media_items`` rows after a scan.

An orphan is a ``media_items`` row whose ``plex_rating_key`` was not seen
during the most recent Plex fetch for the libraries that were successfully
scanned.  Orphan removal is fail-closed: we refuse to trust a scan that
returns suspiciously few items (a Plex auth hiccup returning zero items
looks identical to a genuine mass-deletion, so we need hard floors).

The safeguard thresholds are encapsulated in :class:`OrphanRemovalPolicy`
so they can be overridden in tests or tightened via configuration without
scattering magic numbers across the module.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from mediaman.scanner import repository

logger = logging.getLogger("mediaman")


@dataclass(frozen=True)
class OrphanRemovalPolicy:
    """Fail-closed safeguard thresholds for orphan detection (C31).

    A scan that finds zero items against a previously-populated library is
    almost always a Plex auth hiccup, not a genuine mass-deletion.  These
    thresholds define when we refuse to treat a scan result as
    authoritative.

    Attributes:
        min_items_to_trust: If the current scan found fewer items than
            this floor **and** the previous count met it, skip orphan
            removal.  Prevents a zero-result scan from wiping the DB.
        min_items_for_ratio_check: Only apply the ratio floor when the
            previous item count was at least this large (avoids false
            positives on small libraries).
        min_ratio_to_trust: Minimum fraction of the previous item count
            that the current scan must return before orphan removal is
            trusted.  A huge drop (e.g. 5 of 200) is suspicious.
    """

    min_items_to_trust: int = 5
    min_items_for_ratio_check: int = 50
    min_ratio_to_trust: float = 0.10


# Module-level default used by the engine.  Override by passing a custom
# ``OrphanRemovalPolicy`` instance to :func:`remove_orphans`.
DEFAULT_POLICY = OrphanRemovalPolicy()


def remove_orphans(
    conn: sqlite3.Connection,
    seen_keys: set[str],
    scanned_libs: set[int],
    *,
    policy: OrphanRemovalPolicy = DEFAULT_POLICY,
) -> int:
    """Remove ``media_items`` whose ``plex_rating_key`` is gone from Plex.

    Only considers items belonging to *scanned_libs* (libraries that were
    successfully fetched during this scan run) so items from unreachable
    libraries are never accidentally deleted.

    Args:
        conn: Open SQLite connection.
        seen_keys: Set of ``plex_rating_key`` values observed in the scan.
        scanned_libs: Integer library IDs that were successfully fetched.
        policy: Fail-closed safeguard thresholds.  Defaults to
            :data:`DEFAULT_POLICY`.

    Returns:
        Number of rows deleted.
    """
    if not scanned_libs:
        return 0

    previous_count = repository.count_items_in_libraries(conn, list(scanned_libs))
    current_count = len(seen_keys)

    if current_count < policy.min_items_to_trust and previous_count >= policy.min_items_to_trust:
        logger.warning(
            "engine.orphan_guard.skip reason=below_min_items "
            "current=%d previous=%d threshold=%d scanned_libs=%s — "
            "refusing to remove orphans; admin must verify and "
            "reconcile manually if this is correct.",
            current_count,
            previous_count,
            policy.min_items_to_trust,
            sorted(scanned_libs),
        )
        return 0

    if (
        previous_count > policy.min_items_for_ratio_check
        and current_count < previous_count * policy.min_ratio_to_trust
    ):
        logger.warning(
            "engine.orphan_guard.skip reason=below_ratio "
            "current=%d previous=%d ratio=%.3f min_ratio=%.2f "
            "scanned_libs=%s — refusing to remove orphans; admin "
            "must verify and reconcile manually if this is correct.",
            current_count,
            previous_count,
            (current_count / previous_count) if previous_count else 0.0,
            policy.min_ratio_to_trust,
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
