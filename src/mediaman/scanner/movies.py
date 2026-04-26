"""Movie scanning and deletion eligibility logic."""

from __future__ import annotations

from datetime import datetime

from mediaman.scanner._eligibility import check_age, check_inactivity


def evaluate_movie(
    *,
    added_at: datetime,
    watch_history: list[dict[str, object]],
    min_age_days: int = 30,
    inactivity_days: int = 30,
) -> str:
    """Evaluate whether a movie should be scheduled for deletion.

    A movie is a deletion candidate when it has been in the library long enough
    (``min_age_days``) and either has never been watched or was last watched
    more than ``inactivity_days`` ago.

    Returns ``"skip"`` or ``"schedule_deletion"``.
    """
    if not check_age(added_at, min_age_days):
        return "skip"
    if not check_inactivity(watch_history, inactivity_days):
        return "skip"
    return "schedule_deletion"
