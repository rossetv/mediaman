"""The shared "days left" rule for a scheduled deletion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.services.scheduled_actions.days_left import days_left

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (timedelta(days=3), 3),
        (timedelta(hours=3), 0),
        (timedelta(hours=11, minutes=59), 0),
        (timedelta(hours=12), 1),
        (timedelta(days=1, hours=2), 1),
        (timedelta(0), 0),
        (timedelta(days=-2), -2),
        (timedelta(hours=-1), -1),
    ],
)
def test_the_count_is_calendar_days_so_it_agrees_with_the_date_shown(remaining, expected):
    assert days_left(_NOW + remaining, _NOW) == expected
