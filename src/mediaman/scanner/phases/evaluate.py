"""Evaluate phase — decide whether a media item is eligible for deletion.

Exports one pure eligibility function:

* :func:`evaluate_item` — decides whether a movie or TV season should be
  scheduled for deletion. Movies and seasons share identical
  age-and-inactivity rules, so a single function serves both (the shared
  predicates live in :mod:`mediaman.scanner._eligibility`); the previous
  byte-for-byte ``evaluate_movie`` / ``evaluate_season`` pair was a
  duplication trap where a change to one path would silently diverge from
  the other.

The scan engine calls it from the per-library scan passes in
:mod:`mediaman.scanner._scan_library`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from mediaman.scanner._eligibility import is_inactive, is_old_enough


def evaluate_item(
    *,
    added_at: datetime,
    watch_history: Sequence[Mapping[str, object]],
    min_age_days: int = 30,
    inactivity_days: int = 30,
) -> Literal["skip", "schedule_deletion"]:
    """Evaluate whether a movie or TV season should be scheduled for deletion.

    An item is a deletion candidate when it has been in the library long
    enough (``min_age_days``) **and** either has never been watched or was
    last watched more than ``inactivity_days`` ago. Movies and seasons apply
    the identical rule, so one function serves both.

    Returns ``"skip"`` or ``"schedule_deletion"``.
    """
    if not is_old_enough(added_at, min_age_days):
        return "skip"
    if not is_inactive(watch_history, inactivity_days):
        return "skip"
    return "schedule_deletion"
