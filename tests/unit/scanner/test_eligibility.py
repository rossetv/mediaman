"""Tests for mediaman.scanner._eligibility.

Covers: is_old_enough and is_inactive.
"""

from datetime import UTC, datetime, timedelta

# rationale: is_old_enough and is_inactive are internal scanner helpers not
# re-exported via scanner/__init__.py; they encapsulate deletion-eligibility
# logic (time-based and watch-history thresholds) that is critical enough to
# test directly rather than only through the full scan pipeline.
from mediaman.scanner._eligibility import is_inactive, is_old_enough


def _now():
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# is_old_enough
# ---------------------------------------------------------------------------


class TestCheckAge:
    def test_old_enough_returns_true(self):
        added = _now() - timedelta(days=31)
        assert is_old_enough(added, min_age_days=30) is True

    def test_exactly_at_threshold_returns_true(self):
        # days >= min_age_days — boundary is inclusive.
        added = _now() - timedelta(days=30, hours=1)
        assert is_old_enough(added, min_age_days=30) is True

    def test_too_new_returns_false(self):
        added = _now() - timedelta(days=5)
        assert is_old_enough(added, min_age_days=30) is False

    def test_naive_datetime_treated_as_utc(self):
        # Naive datetimes must not raise; treated as UTC.
        added = datetime.now().replace(tzinfo=None) - timedelta(days=60)
        assert added.tzinfo is None
        assert is_old_enough(added, min_age_days=30) is True

    def test_zero_min_age_always_eligible(self):
        added = _now()  # Just added this instant.
        assert is_old_enough(added, min_age_days=0) is True


# ---------------------------------------------------------------------------
# is_inactive
# ---------------------------------------------------------------------------


class TestCheckInactivity:
    def test_no_history_is_inactive(self):
        # Never watched — eligible.
        assert is_inactive([], inactivity_days=30) is True

    def test_recently_watched_is_not_inactive(self):
        history = [{"viewed_at": _now() - timedelta(days=2)}]
        assert is_inactive(history, inactivity_days=30) is False

    def test_stale_watch_is_inactive(self):
        history = [{"viewed_at": _now() - timedelta(days=60)}]
        assert is_inactive(history, inactivity_days=30) is True

    def test_uses_most_recent_watch_event(self):
        # Two events; only the recent one should matter.
        history = [
            {"viewed_at": _now() - timedelta(days=100)},
            {"viewed_at": _now() - timedelta(days=5)},  # recent
        ]
        assert is_inactive(history, inactivity_days=30) is False

    def test_entries_with_none_viewed_at_filtered(self):
        # One None entry is ignored; the non-None entry governs eligibility.
        history = [
            {"viewed_at": None},
            {"viewed_at": _now() - timedelta(days=5)},  # recent
        ]
        assert is_inactive(history, inactivity_days=30) is False

    def test_exactly_at_threshold_is_inactive(self):
        # days >= inactivity_days — boundary is inclusive.
        history = [{"viewed_at": _now() - timedelta(days=30, hours=1)}]
        assert is_inactive(history, inactivity_days=30) is True

    def test_all_none_viewed_at_does_not_raise(self):
        """D05 finding 12: a watch_history list whose every entry has
        ``viewed_at=None`` used to crash with ``ValueError`` from
        ``max([])``. The function must instead return False (treat as
        recently watched / fail safe) so we never schedule deletion off
        an unusable history.
        """
        history = [{"viewed_at": None}, {"viewed_at": None}]
        # Must not raise.
        result = is_inactive(history, inactivity_days=30)
        assert result is False
