"""The keep label counts whole days left by elapsed time, whatever hour it is read at."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.web.repository.library_query import _display
from mediaman.web.repository.library_query._display import protection_label


def _label(monkeypatch, now: datetime, ends_in: timedelta) -> str | None:
    monkeypatch.setattr(_display, "now_utc", lambda: now)
    return protection_label("snoozed", (now + ends_in).isoformat())


@pytest.mark.parametrize("hour", [1, 12, 23])
def test_keep_ending_in_two_hours_reads_less_than_a_day(monkeypatch, hour):
    now = datetime(2026, 3, 10, hour, 0, tzinfo=UTC)
    assert _label(monkeypatch, now, timedelta(hours=2)) == "Kept for less than a day"


@pytest.mark.parametrize("hour", [1, 12, 23])
def test_keep_ending_in_eleven_days_and_two_hours_reads_eleven_days(monkeypatch, hour):
    now = datetime(2026, 3, 10, hour, 0, tzinfo=UTC)
    assert _label(monkeypatch, now, timedelta(days=11, hours=2)) == "Kept for 11 more days"


def test_keep_ending_in_one_day_and_an_hour_reads_one_more_day(monkeypatch):
    now = datetime(2026, 3, 10, 23, 0, tzinfo=UTC)
    assert _label(monkeypatch, now, timedelta(days=1, hours=1)) == "Kept for 1 more day"
