"""Tests for how many of a season's episodes a person has seen (watch_library.show_seen)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.watch_progress import MarkTarget, mark_watched
from tests.helpers.factories import insert_admin_user, insert_media_item

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def _season(conn, keys: list[str] | None, count: int) -> None:
    insert_media_item(
        conn,
        id="501",
        plex_rating_key="501",
        title="Coast",
        show_title="Coast",
        media_type="tv_season",
        season_number=1,
        show_rating_key="500",
        plex_library_id=1,
    )
    stored = json.dumps(
        [{"key": k, "path": f"/tv/{k}.mkv", "number": n} for n, k in enumerate(keys or [], 1)]
    )
    conn.execute(
        "UPDATE media_items SET episode_count = ?, episode_keys = ? WHERE id = '501'",
        (count, stored if keys is not None else None),
    )
    conn.commit()


def _see(conn, *keys: str) -> None:
    mark_watched(conn, "aoife", [MarkTarget(k, "501", "500", 0) for k in keys], NOW)


def _seen(conn) -> lib.SeasonSeen:
    return lib.show_seen(conn, "aoife", ["1"])["500"].seasons[1]


@pytest.fixture(autouse=True)
def _viewer(conn):
    insert_admin_user(conn, username="aoife", role="viewer")


def test_an_episode_replaced_under_a_new_key_is_not_seen_twice(conn):
    _season(conn, ["200", "101"], 2)
    _see(conn, "100", "200")

    season = _seen(conn)

    assert (season.seen, season.watched) == (1, False)
    assert lib.show_seen(conn, "aoife", ["1"])["500"].watched is False


def test_seen_episodes_since_removed_do_not_stand_in_for_unseen_ones(conn):
    kept = [str(k) for k in range(4, 11)]
    _season(conn, kept, 7)
    _see(conn, "1", "2", "3", *kept[:4])

    show = lib.show_seen(conn, "aoife", ["1"])["500"]

    assert (show.seasons[1].seen, show.watched, show.left) == (4, False, 3)


def test_a_season_with_no_episode_list_counts_every_row(conn):
    _season(conn, None, 2)
    _see(conn, "100", "101")

    assert _seen(conn).watched is True


def test_every_listed_episode_seen_is_watched(conn):
    _season(conn, ["200", "201"], 2)
    _see(conn, "100", "200", "201")

    assert _seen(conn).watched is True
