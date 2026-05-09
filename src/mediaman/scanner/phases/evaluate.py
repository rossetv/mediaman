"""Evaluate phase — decide whether a media item is eligible for deletion.

Exports two pure eligibility functions:

* :func:`evaluate_movie` — decides whether a movie should be scheduled for
  deletion.
* :func:`evaluate_season` — decides whether a TV season should be scheduled
  for deletion.

The scan engine calls them directly from
:meth:`~mediaman.scanner.engine.ScanEngine._scan_movie_library` and
:meth:`~mediaman.scanner.engine.ScanEngine._scan_tv_library`.
"""

from __future__ import annotations

from datetime import datetime

from mediaman.scanner._eligibility import is_inactive, is_old_enough


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
    if not is_old_enough(added_at, min_age_days):
        return "skip"
    if not is_inactive(watch_history, inactivity_days):
        return "skip"
    return "schedule_deletion"


def evaluate_season(
    *,
    added_at: datetime,
    watch_history: list[dict[str, object]],
    min_age_days: int = 30,
    inactivity_days: int = 30,
) -> str:
    """Evaluate whether a TV season should be scheduled for deletion.

    Returns ``"skip"`` or ``"schedule_deletion"``.

    A season is eligible for deletion when all of the following hold:

    - It was added at least ``min_age_days`` ago.
    - Either it has never been watched, or the most recent watch event is older
      than ``inactivity_days``.
    """
    if not is_old_enough(added_at, min_age_days):
        return "skip"
    if not is_inactive(watch_history, inactivity_days):
        return "skip"
    return "schedule_deletion"
