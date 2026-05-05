"""Shared eligibility helpers for movie and TV season scanning.

Both :mod:`movies` and :mod:`tv` apply the same age-and-inactivity
evaluation before deciding whether an item should be scheduled for
deletion. This module extracts that common logic so it is written once
and both callers stay in sync.

Internal to the ``scanner`` package — not part of the public API.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mediaman.core.format import ensure_tz as _ensure_tz


def check_age(added_at: datetime, min_age_days: int) -> bool:
    """Return True if *added_at* is old enough to be eligible for deletion.

    An item whose ``added_at`` timestamp is less than *min_age_days* ago is
    considered too new and should be skipped.

    Args:
        added_at: When the item was added to the library (tz-aware or naive;
            naive is treated as UTC).
        min_age_days: Minimum number of days since *added_at* before an item
            is considered eligible.

    Returns:
        ``True`` when the item has been in the library at least *min_age_days*.
    """
    now = datetime.now(UTC)
    if added_at.tzinfo is None:
        added_at = added_at.replace(tzinfo=UTC)
    return (now - added_at).days >= min_age_days


def check_inactivity(watch_history: list[dict[str, object]], inactivity_days: int) -> bool:
    """Return True if the item has been inactive long enough to be eligible.

    An item with no watch history at all is considered inactive (the
    "never watched" case). An item with watch history is considered
    inactive only when the most recent watch event is older than
    *inactivity_days*.

    A watch-history entry whose ``viewed_at`` is ``None`` (some Plex
    responses include in-progress entries with no completion timestamp)
    is filtered out. If filtering leaves zero usable entries the item
    is treated as having been watched recently — i.e. **not inactive**
    — so we fail safe rather than schedule deletion off an unusable
    history. The previous code raised ``ValueError`` from
    ``max(empty_iter)`` in that case (D05 finding 12).

    Args:
        watch_history: List of watch-history dicts, each containing at
            minimum a ``"viewed_at"`` key with a :class:`datetime` value.
        inactivity_days: Minimum days without a watch event before the
            item is eligible for deletion.

    Returns:
        ``True`` when the item should be considered inactive.
    """
    if not watch_history:
        return True
    now = datetime.now(UTC)
    timestamps = [
        _ensure_tz(viewed)
        for h in watch_history
        if isinstance(viewed := h.get("viewed_at"), datetime)
    ]
    if not timestamps:
        # Watch history exists but every entry is missing a timestamp.
        # Treat as "watched recently" (fail safe) so we never queue a
        # deletion off a history we cannot reason about. The
        # alternative — falling through to the "no history" branch —
        # would queue the item for deletion, which is more dangerous
        # than briefly skipping a possibly-stale entry.
        return False
    most_recent = max(timestamps)
    return (now - most_recent).days >= inactivity_days
