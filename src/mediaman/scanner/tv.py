"""TV season scanning and deletion eligibility logic."""

from __future__ import annotations

from datetime import datetime

from mediaman.scanner._eligibility import check_age, check_inactivity


def evaluate_season(
    *,
    added_at: datetime,
    episode_count: int,
    watch_history: list[dict[str, object]],
    has_future_episodes: bool,
    min_age_days: int = 30,
    inactivity_days: int = 30,
) -> str:
    """Evaluate whether a TV season should be scheduled for deletion.

    Returns ``"skip"`` or ``"schedule_deletion"``.

    A season is eligible for deletion when all of the following hold:

    - It was added at least ``min_age_days`` ago.
    - It has no future episodes (i.e. the season is complete / not still airing).
    - Either it has never been watched, or the most recent watch event is older
      than ``inactivity_days``.
    """
    if not check_age(added_at, min_age_days):
        return "skip"
    if has_future_episodes:
        return "skip"
    if not check_inactivity(watch_history, inactivity_days):
        return "skip"
    return "schedule_deletion"
