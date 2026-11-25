"""Tests for each person's Watch progress in mediaman.web.repository.watch_progress."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.scanner.repository import rekeyed
from mediaman.scanner.repository.media_items import delete_media_items
from mediaman.web.repository.watch_progress import (
    _CONTINUE_CANDIDATES,
    MarkTarget,
    continue_watching,
    dismiss,
    get_progress,
    mark_watched,
    restore_marks,
    save_progress,
    show_progress,
    undismiss,
    unmark_watched,
)
from tests.helpers.factories import insert_media_item

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
    credits=None,
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
        credits_s=credits,
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

    def test_reaching_the_credits_marks_watched(self, users):
        assert _save(users, position=4999.0, credits=5000.0) is False
        assert _save(users, position=5000.0, credits=5000.0) is True
        assert get_progress(users, "aoife", ["m1"])["m1"].seen is True

    def test_credits_after_ninety_percent_do_not_put_watched_off(self, users):
        assert _save(users, position=5400.0, credits=5800.0) is True

    def test_credits_leave_the_played_time_cleanup_asks_for_as_it_was(self, users):
        _save(users, duration=300.0, position=200.0, credits=200.0, played=269.0)
        assert _credited(users, "m1") is None
        _save(users, duration=300.0, position=200.0, credits=200.0, played=270.0)
        assert _credited(users, "m1") is not None

    def test_playing_a_watched_item_from_the_start_makes_it_in_progress(self, users):
        _save(users, position=5900.0)
        assert _save(users, position=12.0) is False
        assert get_progress(users, "aoife", ["m1"])["m1"].watched is False

    def test_a_replay_keeps_the_title_seen(self, users):
        _save(users, position=5900.0)
        _save(users, position=12.0, now=NOON + timedelta(days=1))
        assert get_progress(users, "aoife", ["m1"])["m1"].seen is True

    def test_a_replay_of_a_title_watched_before_seen_times_were_kept_keeps_it_seen(self, users):
        _save(users, position=5900.0)
        users.execute("UPDATE watch_progress SET seen_at = NULL")
        users.commit()
        _save(users, position=12.0, now=NOON + timedelta(days=1))
        assert get_progress(users, "aoife", ["m1"])["m1"].seen is True

    def test_a_title_only_begun_is_not_seen(self, users):
        _save(users, position=600.0)
        assert get_progress(users, "aoife", ["m1"])["m1"].seen is False


def _target(key, show=None):
    return MarkTarget(key, "s1" if show else key, show, 6000.0)


def _mark(conn, *keys, show=None, username="aoife", now=NOON):
    return mark_watched(conn, username, [_target(k, show) for k in keys], now)


def _rows(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM watch_progress ORDER BY rating_key")]


def _seen(conn, *keys, username="aoife"):
    found = get_progress(conn, username, keys)
    return [k for k in keys if k in found and found[k].seen]


class TestMarks:
    def test_a_mark_keeps_the_position_and_the_time_of_the_last_play(self, users):
        _save(users, position=600.0)
        _mark(users, "m1", now=NOON + timedelta(days=1))

        row = get_progress(users, "aoife", ["m1"])["m1"]
        assert (row.watched, row.position_s, row.updated_at) == (True, 600.0, NOON.isoformat())

    def test_a_save_from_a_player_still_open_does_not_undo_a_mark(self, users):
        _save(users, position=600.0)
        _mark(users, "m1")
        _save(users, position=610.0, now=NOON + timedelta(seconds=10))
        _save(users, position=620.0, now=NOON + timedelta(seconds=20))
        assert _seen(users, "m1") == ["m1"]

    def test_marking_is_one_persons_own(self, users):
        _mark(users, "m1")
        _mark(users, "m2", username="admin")
        unmark_watched(users, "admin", "movie", "m1")
        assert (_seen(users, "m1", "m2"), _seen(users, "m1", "m2", username="admin")) == (
            ["m1"],
            ["m2"],
        )

    def test_unmarking_keeps_the_row_and_a_place_short_of_the_end(self, users):
        _save(users, position=600.0, played=600.0)
        _mark(users, "m1")
        unmark_watched(users, "aoife", "movie", "m1")

        row = get_progress(users, "aoife", ["m1"])["m1"]
        assert (row.seen, row.position_s) == (False, 600.0)
        assert _credited(users, "m1") == NOON.isoformat()

    def test_unmarking_a_finished_title_starts_it_over(self, users):
        _save(users, position=5900.0)
        unmark_watched(users, "aoife", "movie", "m1")
        assert get_progress(users, "aoife", ["m1"])["m1"].position_s == 0

    def test_unmarking_a_show_leaves_its_specials(self, users):
        users.execute(
            "INSERT INTO media_items (id, title, media_type, season_number, plex_library_id,"
            " plex_rating_key, added_at, file_path, file_size_bytes)"
            " VALUES ('s0', 'Specials', 'tv_season', 0, 1, 's0', '', '', 0)"
        )
        users.commit()
        _mark(users, "e1", "e2", show="sh")
        mark_watched(users, "aoife", [MarkTarget("sp1", "s0", "sh", 60.0)], NOON)

        unmark_watched(users, "aoife", "show", "sh")

        assert _seen(users, "e1", "e2", "sp1") == ["sp1"]

    def test_unmarking_a_season_finds_its_episodes_saved_without_it(self, users):
        insert_media_item(users, id="s1", plex_rating_key="s1", title="S", show_rating_key="sh")
        listed = '[{"key": "e1"}, {"key": "e2"}, {"key": "e3"}]'
        users.execute("UPDATE media_items SET episode_keys = ? WHERE id = 's1'", (listed,))
        users.commit()
        _mark(users, "e1", "e2", "e3", "e9", show="sh")
        users.execute("UPDATE watch_progress SET media_item_id = NULL WHERE rating_key = 'e2'")
        users.execute("UPDATE watch_progress SET media_item_id = 's9' WHERE rating_key = 'e3'")
        users.execute("UPDATE watch_progress SET media_item_id = 's9' WHERE rating_key = 'e9'")
        users.commit()

        unmark_watched(users, "aoife", "season", "s1")

        assert _seen(users, "e1", "e2", "e3", "e9") == ["e9"]

    def test_undoing_a_mark_restores_only_the_rows_it_changed(self, users):
        _save(users, rating_key="e1", media_item_id="s1", show="sh", position=5900.0)
        _save(users, rating_key="e2", media_item_id="s1", show="sh", position=900.0)
        before = _rows(users)

        changes = _mark(users, "e1", "e2", "e3", show="sh", now=NOON + timedelta(hours=1))
        restore_marks(users, "aoife", changes)

        assert [r for r in _rows(users) if r[1] != "e3"] == before
        assert _seen(users, "e1", "e2", "e3") == ["e1"]
        assert [c.progress.rating_key for c in continue_watching(users, "aoife")] == ["e1"]

    def test_undoing_an_unmark_brings_the_place_back(self, users):
        _save(users, position=5900.0)
        before = _rows(users)
        restore_marks(users, "aoife", unmark_watched(users, "aoife", "movie", "m1"))
        assert _rows(users) == before

    def test_undo_leaves_a_play_that_came_after_it(self, users):
        _save(users, position=5900.0)
        changes = unmark_watched(users, "aoife", "movie", "m1")
        _save(users, position=300.0, now=NOON + timedelta(minutes=1))

        restore_marks(users, "aoife", changes)

        row = get_progress(users, "aoife", ["m1"])["m1"]
        assert (row.watched, row.position_s, row.seen) == (False, 300.0, True)

    def test_undo_leaves_a_title_marked_again_since(self, users):
        first = _mark(users, "m1")
        unmark_watched(users, "aoife", "movie", "m1")
        _mark(users, "m1", now=NOON + timedelta(minutes=1))
        restore_marks(users, "aoife", first)
        assert _seen(users, "m1") == ["m1"]


class TestMarksInContinueWatching:
    def test_marking_an_old_episode_does_not_move_where_the_show_resumes(self, users):
        _save(users, rating_key="e5", show="sh", position=600.0)
        _mark(users, "e1", show="sh", now=NOON + timedelta(days=1))
        [card] = continue_watching(users, "aoife")
        assert (card.progress.rating_key, card.find_next) == ("e5", False)

    def test_a_show_never_played_stays_out_when_marked_and_unmarked(self, users):
        _mark(users, "e1", "e2", show="sh")
        assert continue_watching(users, "aoife") == []
        unmark_watched(users, "aoife", "show", "sh")
        assert continue_watching(users, "aoife") == []

    def test_a_show_marked_not_watched_from_end_to_end_leaves(self, users):
        _save(users, rating_key="e1", show="sh", position=5900.0)
        unmark_watched(users, "aoife", "show", "sh")
        assert continue_watching(users, "aoife") == []

    def test_a_marked_movie_leaves(self, users):
        _save(users, position=600.0)
        _mark(users, "m1")
        assert continue_watching(users, "aoife") == []


class TestDismiss:
    def test_a_dismissed_title_is_hidden_until_it_is_played_again(self, users):
        _save(users, rating_key="m1", position=600.0)
        _save(users, rating_key="e1", show="sh", position=600.0)
        dismiss(users, "aoife", "sh", NOON + timedelta(minutes=1))
        assert [c.progress.rating_key for c in continue_watching(users, "aoife")] == ["m1"]
        assert get_progress(users, "aoife", ["e1"])["e1"].position_s == 600.0

        _save(users, rating_key="e2", show="sh", position=60.0, now=NOON + timedelta(minutes=2))
        assert [c.progress.rating_key for c in continue_watching(users, "aoife")] == ["e2", "m1"]

    def test_undismissing_and_dismissing_again_work(self, users):
        _save(users, position=600.0)
        dismiss(users, "aoife", "m1", NOON)
        undismiss(users, "aoife", "m1")
        assert len(continue_watching(users, "aoife")) == 1
        dismiss(users, "aoife", "m1", NOON)
        _save(users, position=700.0, now=NOON + timedelta(minutes=1))
        dismiss(users, "aoife", "m1", NOON + timedelta(minutes=2))
        assert continue_watching(users, "aoife") == []

    def test_a_dismissal_is_one_persons_own(self, users):
        _save(users, position=600.0)
        _save(users, username="admin", position=600.0)
        dismiss(users, "admin", "m1", NOON + timedelta(minutes=1))
        undismiss(users, "aoife", "m1")
        assert len(continue_watching(users, "aoife")) == 1
        assert continue_watching(users, "admin") == []

    def test_a_mark_does_not_bring_a_dismissed_show_back(self, users):
        _save(users, rating_key="e1", show="sh", position=600.0)
        dismiss(users, "aoife", "sh", NOON + timedelta(minutes=1))
        _mark(users, "e1", show="sh", now=NOON + timedelta(minutes=2))
        assert continue_watching(users, "aoife") == []

    def test_a_dismissal_goes_once_its_title_is_gone_for_good(self, users):
        now = NOON
        for key in ("inlib", "aside", "gone"):
            insert_media_item(users, id=key, plex_rating_key=key, title=key)
            _save(users, rating_key=key, position=600.0, now=now)
            dismiss(users, "aoife", key, now)
        with users:
            rekeyed.save_pruned_progress(users, ["aside"])
            delete_media_items(users, ["aside", "gone"])

        dismiss(users, "admin", "inlib", now)

        left = users.execute("SELECT username, title_key FROM watch_dismissed ORDER BY 1, 2")
        assert [tuple(r) for r in left] == [
            ("admin", "inlib"),
            ("aoife", "aside"),
            ("aoife", "inlib"),
        ]

    def test_dismissed_titles_do_not_use_up_the_titles_read(self, users):
        _save(users, rating_key="old", position=600.0)
        for i in range(_CONTINUE_CANDIDATES):
            _save(users, rating_key=f"n{i}", position=600.0, now=NOON + timedelta(seconds=1))
            dismiss(users, "aoife", f"n{i}", NOON + timedelta(seconds=1))
        assert [c.progress.rating_key for c in continue_watching(users, "aoife")] == ["old"]


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


class TestManyKeys:
    def test_progress_is_found_for_more_keys_than_one_query_can_bind(self, users):
        _save(users, rating_key="m1", position=60.0)
        _save(users, rating_key="m1100", position=60.0)
        # A connection that binds at most 600 values per statement.
        users.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 600)

        found = get_progress(users, "aoife", [f"m{i}" for i in range(1, 1201)])

        assert set(found) == {"m1", "m1100"}


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

    def test_a_long_binge_does_not_crowd_out_other_shows_and_movies(self, users):
        _save(users, rating_key="old-movie", position=600.0, now=NOON)
        users.executemany(
            "INSERT INTO watch_progress (username, rating_key, media_item_id, show_rating_key,"
            " position_s, duration_s, watched, updated_at) VALUES ('aoife', ?, ?, ?, 600, 6000, 0, ?)",
            [
                ("other-ep", "other-ep", "other", (NOON + timedelta(seconds=1)).isoformat()),
                *[
                    (f"e{i}", f"e{i}", "binge", (NOON + timedelta(minutes=1 + i)).isoformat())
                    for i in range(_CONTINUE_CANDIDATES + 50)
                ],
            ],
        )
        users.commit()

        cards = continue_watching(users, "aoife")

        assert [c.progress.rating_key for c in cards] == [
            f"e{_CONTINUE_CANDIDATES + 49}",
            "other-ep",
            "old-movie",
        ]

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


def test_a_failed_save_leaves_no_transaction_open(users):
    with pytest.raises(sqlite3.IntegrityError):
        _save(users, username="ghost")
    assert not users.in_transaction


def test_undoing_a_mark_that_made_a_row_leaves_a_play_since(users):
    [change] = mark_watched(users, "aoife", [MarkTarget("m1", "m1", None, 0)], NOON)
    save_progress(
        users,
        username="aoife",
        rating_key="m1",
        media_item_id="m1",
        show_rating_key=None,
        position_s=600,
        duration_s=6000,
        played_s=20,
        now=NOON + timedelta(minutes=1),
    )

    restore_marks(users, "aoife", [change])

    assert get_progress(users, "aoife", ["m1"])["m1"].position_s == 600
    assert change.new


def test_undoing_a_mark_leaves_a_row_that_was_there_though_empty(users):
    target = MarkTarget("m1", "m1", None, 0)
    mark_watched(users, "aoife", [target], NOON)
    unmark_watched(users, "aoife", "movie", "m1")

    restore_marks(users, "aoife", mark_watched(users, "aoife", [target], NOON))

    assert list(get_progress(users, "aoife", ["m1"])) == ["m1"]
