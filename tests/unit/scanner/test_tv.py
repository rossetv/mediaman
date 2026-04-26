"""Tests for TV season scanning logic."""

from datetime import datetime, timedelta, timezone

from mediaman.scanner.tv import evaluate_season


def _now():
    return datetime.now(timezone.utc)


class TestEvaluateSeason:
    def test_skip_recently_added(self):
        result = evaluate_season(
            added_at=_now() - timedelta(days=10),
            episode_count=10,
            watch_history=[],
            has_future_episodes=False,
            min_age_days=30,
            inactivity_days=30,
        )
        assert result == "skip"

    def test_delete_old_never_watched(self):
        result = evaluate_season(
            added_at=_now() - timedelta(days=60),
            episode_count=10,
            watch_history=[],
            has_future_episodes=False,
            min_age_days=30,
            inactivity_days=30,
        )
        assert result == "schedule_deletion"

    def test_skip_still_airing(self):
        result = evaluate_season(
            added_at=_now() - timedelta(days=60),
            episode_count=10,
            watch_history=[],
            has_future_episodes=True,
            min_age_days=30,
            inactivity_days=30,
        )
        assert result == "skip"

    def test_delete_all_watched_and_inactive(self):
        watches = [
            {"viewed_at": _now() - timedelta(days=40), "episode_title": f"Ep {i}"}
            for i in range(10)
        ]
        result = evaluate_season(
            added_at=_now() - timedelta(days=90),
            episode_count=10,
            watch_history=watches,
            has_future_episodes=False,
            min_age_days=30,
            inactivity_days=30,
        )
        assert result == "schedule_deletion"

    def test_skip_partially_watched_recently(self):
        watches = [{"viewed_at": _now() - timedelta(days=5), "episode_title": "Ep 1"}]
        result = evaluate_season(
            added_at=_now() - timedelta(days=60),
            episode_count=10,
            watch_history=watches,
            has_future_episodes=False,
            min_age_days=30,
            inactivity_days=30,
        )
        assert result == "skip"

    def test_delete_partially_watched_inactive(self):
        watches = [
            {"viewed_at": _now() - timedelta(days=50), "episode_title": "Ep 1"},
            {"viewed_at": _now() - timedelta(days=48), "episode_title": "Ep 2"},
        ]
        result = evaluate_season(
            added_at=_now() - timedelta(days=90),
            episode_count=10,
            watch_history=watches,
            has_future_episodes=False,
            min_age_days=30,
            inactivity_days=30,
        )
        assert result == "schedule_deletion"
