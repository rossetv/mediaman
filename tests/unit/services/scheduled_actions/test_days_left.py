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
        (timedelta(days=2, seconds=1), 3),
        (timedelta(hours=3), 1),
        (timedelta(seconds=1), 1),
        (timedelta(0), 0),
        (timedelta(days=-2), 0),
    ],
)
def test_a_started_day_counts_and_a_passed_deadline_is_zero(remaining, expected):
    assert days_left(_NOW + remaining, _NOW) == expected
