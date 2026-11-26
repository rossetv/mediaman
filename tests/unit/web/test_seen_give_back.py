"""Giving back a season of a show marked watched outside the library."""

from __future__ import annotations

from datetime import UTC, datetime

from mediaman.web.repository import seen_titles
from mediaman.web.repository.ratings import RatedTitle, find_user_id
from tests.helpers.factories import insert_admin_user

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
KEY = ("tv", 44)


def _marked(conn, *, in_library, through=2, episodes=None):
    insert_admin_user(conn, username="aoife", role="viewer")
    user = find_user_id(conn, "aoife")
    seen_titles.mark(
        conn, user, RatedTitle("tv", 44), through, NOW, in_library=in_library, episodes=episodes
    )
    return user


def _part(episode, listed):
    return seen_titles.ShowPart("500", 1, episode, {1: frozenset(listed)})


def test_a_whole_season_taken_back_and_given_back_is_whole_again(conn):
    user = _marked(conn, in_library=False)
    original = seen_titles.get_mark(conn, user, KEY)

    seen_titles.change_part(conn, user, KEY, _part(None, {1, 2}), seen=False)
    seen_titles.change_part(conn, user, KEY, _part(None, {1, 2, 3}), seen=True)

    assert seen_titles.get_mark(conn, user, KEY) == original


def test_a_season_taken_back_by_one_episode_and_given_back_whole_is_whole_again(conn):
    user = _marked(conn, in_library=False)
    original = seen_titles.get_mark(conn, user, KEY)

    seen_titles.change_part(conn, user, KEY, _part(1, {1, 2, 3}), seen=False)
    seen_titles.change_part(conn, user, KEY, _part(None, {1, 2, 3}), seen=True)

    assert seen_titles.get_mark(conn, user, KEY) == original


def test_a_season_given_back_in_part_reaches_only_those_episodes(conn):
    user = _marked(conn, in_library=False)
    seen_titles.change_part(conn, user, KEY, _part(None, {1, 2}), seen=False)

    seen_titles.change_part(conn, user, KEY, _part(1, {1, 2, 3}), seen=True)

    after = seen_titles.get_mark(conn, user, KEY)
    assert (after.episodes[1], after.partly) == ({1}, True)


def test_a_season_that_was_never_whole_is_given_back_as_the_episodes_listed(conn):
    user = _marked(conn, in_library=True, episodes={1: {1}})

    seen_titles.change_part(conn, user, KEY, _part(None, {1, 2}), seen=True)

    assert seen_titles.get_mark(conn, user, KEY).episodes[1] == {1, 2}
