"""Tests for the time-based abandon thresholds and gate."""

from __future__ import annotations


def test_abandon_button_threshold_is_ten_hours():
    from mediaman.services.arr.auto_abandon import _ABANDON_BUTTON_VISIBLE_AFTER_SECONDS

    assert _ABANDON_BUTTON_VISIBLE_AFTER_SECONDS == 10 * 3600


def test_auto_abandon_threshold_is_seven_days():
    from mediaman.services.arr.auto_abandon import _AUTO_ABANDON_AFTER_SECONDS

    assert _AUTO_ABANDON_AFTER_SECONDS == 7 * 86_400
