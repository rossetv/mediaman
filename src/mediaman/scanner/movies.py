"""Movie scanning and deletion eligibility logic."""

from datetime import datetime, timezone

from mediaman.services.format import ensure_tz as _ensure_tz


def evaluate_movie(
    *,
    added_at: datetime,
    watch_history: list[dict],
    min_age_days: int = 30,
    inactivity_days: int = 30,
) -> str:
    """Evaluate whether a movie should be scheduled for deletion.

    A movie is a deletion candidate when it has been in the library long enough
    (min_age_days) and either has never been watched or was last watched more
    than inactivity_days ago.

    Returns "skip" or "schedule_deletion".
    """
    now = datetime.now(timezone.utc)
    if added_at.tzinfo is None:
        added_at = added_at.replace(tzinfo=timezone.utc)
    if (now - added_at).days < min_age_days:
        return "skip"
    if not watch_history:
        return "schedule_deletion"
    most_recent = max(_ensure_tz(h["viewed_at"]) for h in watch_history)
    if (now - most_recent).days < inactivity_days:
        return "skip"
    return "schedule_deletion"


