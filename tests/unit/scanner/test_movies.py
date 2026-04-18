"""Tests for movie scanning logic."""

from datetime import datetime, timedelta, timezone

import pytest

from mediaman.scanner.movies import evaluate_movie


def _now():
    return datetime.now(timezone.utc)


class TestEvaluateMovie:
    def test_skip_recently_added(self):
        result = evaluate_movie(added_at=_now() - timedelta(days=10), watch_history=[], min_age_days=30, inactivity_days=30)
        assert result == "skip"

    def test_delete_old_never_watched(self):
        result = evaluate_movie(added_at=_now() - timedelta(days=60), watch_history=[], min_age_days=30, inactivity_days=30)
        assert result == "schedule_deletion"

    def test_skip_recently_watched(self):
        result = evaluate_movie(added_at=_now() - timedelta(days=60), watch_history=[{"viewed_at": _now() - timedelta(days=5)}], min_age_days=30, inactivity_days=30)
        assert result == "skip"

    def test_delete_watched_long_ago(self):
        result = evaluate_movie(added_at=_now() - timedelta(days=60), watch_history=[{"viewed_at": _now() - timedelta(days=45)}], min_age_days=30, inactivity_days=30)
        assert result == "schedule_deletion"

    def test_skip_when_any_recent_watch(self):
        result = evaluate_movie(
            added_at=_now() - timedelta(days=90),
            watch_history=[{"viewed_at": _now() - timedelta(days=60)}, {"viewed_at": _now() - timedelta(days=5)}],
            min_age_days=30, inactivity_days=30,
        )
        assert result == "skip"
