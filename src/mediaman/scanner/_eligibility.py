"""Shared eligibility helpers for movie and TV season scanning.

Both :mod:`movies` and :mod:`tv` apply the same age-and-inactivity
evaluation before deciding whether an item should be scheduled for
deletion. This module extracts that common logic so it is written once
and both callers stay in sync.

Internal to the ``scanner`` package — not part of the public API.
"""
from __future__ import annotations

from datetime import datetime, timezone

from mediaman.services.format import ensure_tz as _ensure_tz


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
    now = datetime.now(timezone.utc)
    if added_at.tzinfo is None:
        added_at = added_at.replace(tzinfo=timezone.utc)
    return (now - added_at).days >= min_age_days


def check_inactivity(
    watch_history: list[dict[str, object]], inactivity_days: int
) -> bool:
    """Return True if the item has been inactive long enough to be eligible.

    An item with no watch history at all is considered inactive (the
    "never watched" case). An item with watch history is considered
    inactive only when the most recent watch event is older than
    *inactivity_days*.

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
    now = datetime.now(timezone.utc)
    most_recent = max(
        _ensure_tz(h["viewed_at"])
        for h in watch_history
        if h.get("viewed_at") is not None
    )
    return (now - most_recent).days >= inactivity_days
