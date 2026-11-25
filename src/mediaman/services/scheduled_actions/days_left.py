"""The "days left" count shown for a scheduled deletion, in the email and on the keep page."""

from __future__ import annotations

from datetime import datetime

from mediaman.core.format import calendar_days_until


def days_left(execute_at: datetime, now: datetime) -> int:
    """UTC calendar days from *now* to *execute_at*; below 0 once it has passed.

    Calendar days, not elapsed time, so the count always agrees with the date shown beside it:
    0 means the deadline is later today, 1 means tomorrow. Past it, also earlier today, the
    deletion pass may take the title any day.
    """
    days = calendar_days_until(execute_at, now)
    return min(days, -1) if execute_at < now else days
