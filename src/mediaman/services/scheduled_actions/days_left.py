"""The "days left" count shown for a scheduled deletion, in the email and on the keep page."""

from __future__ import annotations

import math
from datetime import datetime

_SECONDS_PER_DAY = 86400


def days_left(execute_at: datetime, now: datetime) -> int:
    """Whole days until *execute_at*, rounded up, never below 0.

    The deletion pass runs an item once ``execute_at <= now``, so a started day still has to
    elapse before it is due: "N days left" means it is due within N days, never sooner than
    N-1 days away. A deadline that has passed counts as 0.
    """
    return max(0, math.ceil((execute_at - now).total_seconds() / _SECONDS_PER_DAY))
