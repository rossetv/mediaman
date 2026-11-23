"""Tests for each person's Watch progress in mediaman.web.repository.watch_progress."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.web.repository.watch_progress import (
    continue_watching,
    get_progress,
    save_progress,
    show_progress,
)

NOON = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def users(conn):
    # Plain rows rather than real accounts: progress only needs the username
    # to exist, and hashing a password costs a quarter of a second each.
    conn.executemany(
        "INSERT INTO admin_users (username, password_hash, created_at, role) VALUES (?, '', ?, ?)",
        [("admin", NOON.isoformat(), "admin"), ("aoife", NOON.isoformat(), "viewer")],
    )
    conn.commit()
    return conn


def _save(
    conn,
    *,
    username="aoife",
    rating_key="m1",
    media_item_id=None,
    show=None,
    position=0.0,
    duration=6000.0,
    played=0.0,
    now=NOON,
):
    return save_progress(
        conn,
        username=username,
        rating_key=rating_key,
        media_item_id=media_item_id or rating_key,
        show_rating_key=show,
        position_s=position,
        duration_s=duration,
        played_s=played,
        now=now,
    )


def _credited(conn, rating_key):
    return conn.execute(
        "SELECT credited_at FROM watch_progress WHERE rating_key = ?", (rating_key,)
    ).fetchone()["credited_at"]


class TestWatched:
    def test_passing_ninety_percent_marks_watched(self, users):
        assert _save(users, position=5399.0) is False
        assert _save(users, position=5400.0) is True
        assert get_progress(users, "aoife", ["m1"])["m1"].watched is True

    def test_playing_a_watched_item_from_the_start_makes_it_in_progress(self, users):
        _save(users, position=5900.0)
        assert _save(users, position=12.0) is False
        assert get_progress(users, "aoife", ["m1"])["m1"].watched is False


class TestCredit:
    def test_a_few_seconds_of_play_earns_no_credit(self, users):
        _save(users, position=4000.0, played=20.0)
        assert _credited(users, "m1") is None

    def test_five_minutes_of_play_credits_the_item(self, users):
        _save(users, position=310.0, played=300.0)
        assert _credited(users, "m1") == NOON.isoformat()

    def test_a_short_item_is_credited_only_when_played_to_the_end(self, users):
        # A 4-minute item: 90% of it (216 s) is the threshold, not 300 s.
        _save(users, rating_key="short", duration=240.0, position=200.0, played=200.0)
        assert _credited(users, "short") is None
        _save(users, rating_key="short", duration=240.0, position=230.0, played=220.0)
        assert _credited(users, "short") is not None

    def test_a_later_short_save_keeps_the_earlier_credit(self, users):
        _save(users, position=400.0, played=400.0)
        first = _credited(users, "m1")
        _save(users, position=410.0, played=5.0, now=NOON + timedelta(hours=1))
        assert first is not None
        assert _credited(users, "m1") == first


class TestPerPerson:
    def test_one_person_never_sees_another_persons_progress(self, users):
        _save(users, username="admin", rating_key="m1", position=600.0)
        _save(users, username="admin", rating_key="e1", show="sh", position=600.0)

        assert get_progress(users, "aoife", ["m1", "e1"]) == {}
        assert show_progress(users, "aoife", "sh") == {}
        assert continue_watching(users, "aoife") == []
        assert set(get_progress(users, "admin", ["m1", "e1"])) == {"m1", "e1"}


class TestContinueWatching:
    def test_one_card_per_show_most_recent_first(self, users):
        t = NOON
        _save(users, rating_key="e1", show="sh", position=600.0, now=t)
        _save(users, rating_key="m1", position=600.0, now=t + timedelta(minutes=1))
        _save(users, rating_key="e2", show="sh", position=700.0, now=t + timedelta(minutes=2))

        cards = continue_watching(users, "aoife")

        assert [(c.progress.rating_key, c.find_next) for c in cards] == [
            ("e2", False),
            ("m1", False),
        ]

    def test_a_show_whose_last_episode_is_finished_asks_for_the_next(self, users):
        _save(users, rating_key="e1", show="sh", position=100.0, now=NOON)
        _save(users, rating_key="e2", show="sh", position=5800.0, now=NOON + timedelta(minutes=1))

        [card] = continue_watching(users, "aoife")

        assert (card.progress.rating_key, card.progress.show_rating_key) == ("e2", "sh")
        assert card.find_next is True

    def test_watched_and_barely_started_movies_are_left_out(self, users):
        _save(users, rating_key="done", position=5800.0)
        _save(users, rating_key="peek", position=10.0)
        assert continue_watching(users, "aoife") == []


class TestClamping:
    @pytest.mark.parametrize(
        ("position", "duration", "expected"),
        [
            (-50.0, 6000.0, (0.0, 6000.0)),
            (9000.0, 6000.0, (6000.0, 6000.0)),
            (float("nan"), float("nan"), (0.0, 0.0)),
            (float("inf"), -1.0, (0.0, 0.0)),
        ],
    )
    def test_absurd_values_are_clamped(self, users, position, duration, expected):
        watched = _save(users, position=position, duration=duration, played=float("nan"))
        p = get_progress(users, "aoife", ["m1"])["m1"]
        assert (p.position_s, p.duration_s) == expected
        assert watched is (expected == (6000.0, 6000.0))
        assert _credited(users, "m1") is None
