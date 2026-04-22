"""TV season scanning and deletion eligibility logic."""

from datetime import datetime, timezone

from mediaman.services.format import ensure_tz as _ensure_tz


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

    Returns "skip" or "schedule_deletion".

    A season is eligible for deletion when all of the following hold:
    - It was added at least ``min_age_days`` ago.
    - It has no future episodes (i.e. the season is complete / not still airing).
    - Either it has never been watched, or the most recent watch event is older
      than ``inactivity_days``.
    """
    now = datetime.now(timezone.utc)
    if added_at.tzinfo is None:
        added_at = added_at.replace(tzinfo=timezone.utc)
    if (now - added_at).days < min_age_days:
        return "skip"
    if has_future_episodes:
        return "skip"
    if not watch_history:
        return "schedule_deletion"
    most_recent = max(_ensure_tz(h["viewed_at"]) for h in watch_history)
    if (now - most_recent).days < inactivity_days:
        return "skip"
    return "schedule_deletion"


