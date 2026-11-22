"""The "days left" count shown for a scheduled deletion, in the email and on the keep page."""

from __future__ import annotations

from datetime import datetime

from mediaman.core.format import calendar_days_until


def days_left(execute_at: datetime, now: datetime) -> int:
    """UTC calendar days from *now* to *execute_at*, never below 0.

    Calendar days, not elapsed time, so the count always agrees with the date shown beside it:
    0 means the deadline is later today, 1 means tomorrow. A deadline that has passed counts as 0.
    """
    return max(0, calendar_days_until(execute_at, now))
