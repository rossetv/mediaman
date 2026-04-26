"""Tests for mediaman.scanner._eligibility.

Covers: check_age and check_inactivity.
"""

from datetime import datetime, timedelta, timezone

from mediaman.scanner._eligibility import check_age, check_inactivity


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# check_age
# ---------------------------------------------------------------------------


class TestCheckAge:
    def test_old_enough_returns_true(self):
        added = _now() - timedelta(days=31)
        assert check_age(added, min_age_days=30) is True

    def test_exactly_at_threshold_returns_true(self):
        # days >= min_age_days — boundary is inclusive.
        added = _now() - timedelta(days=30, hours=1)
        assert check_age(added, min_age_days=30) is True

    def test_too_new_returns_false(self):
        added = _now() - timedelta(days=5)
        assert check_age(added, min_age_days=30) is False

    def test_naive_datetime_treated_as_utc(self):
        # Naive datetimes must not raise; treated as UTC.
        added = datetime.now().replace(tzinfo=None) - timedelta(days=60)
        assert added.tzinfo is None
        assert check_age(added, min_age_days=30) is True

    def test_zero_min_age_always_eligible(self):
        added = _now()  # Just added this instant.
        assert check_age(added, min_age_days=0) is True


# ---------------------------------------------------------------------------
# check_inactivity
# ---------------------------------------------------------------------------


class TestCheckInactivity:
    def test_no_history_is_inactive(self):
        # Never watched — eligible.
        assert check_inactivity([], inactivity_days=30) is True

    def test_recently_watched_is_not_inactive(self):
        history = [{"viewed_at": _now() - timedelta(days=2)}]
        assert check_inactivity(history, inactivity_days=30) is False

    def test_stale_watch_is_inactive(self):
        history = [{"viewed_at": _now() - timedelta(days=60)}]
        assert check_inactivity(history, inactivity_days=30) is True

    def test_uses_most_recent_watch_event(self):
        # Two events; only the recent one should matter.
        history = [
            {"viewed_at": _now() - timedelta(days=100)},
            {"viewed_at": _now() - timedelta(days=5)},  # recent
        ]
        assert check_inactivity(history, inactivity_days=30) is False

    def test_entries_with_none_viewed_at_filtered(self):
        # One None entry is ignored; the non-None entry governs eligibility.
        history = [
            {"viewed_at": None},
            {"viewed_at": _now() - timedelta(days=5)},  # recent
        ]
        assert check_inactivity(history, inactivity_days=30) is False

    def test_exactly_at_threshold_is_inactive(self):
        # days >= inactivity_days — boundary is inclusive.
        history = [{"viewed_at": _now() - timedelta(days=30, hours=1)}]
        assert check_inactivity(history, inactivity_days=30) is True
