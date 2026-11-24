"""How long a title has been searching, as auto-abandon and the Abandon button see it."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.db import init_db
from mediaman.services.arr.auto_abandon import maybe_auto_abandon
from mediaman.services.arr.fetcher import fetch_arr_queue
from mediaman.services.arr.fetcher._searching_since import (
    forget_searching_since,
    stamp_searching_since,
)
from mediaman.services.arr.search_trigger import reset_search_triggers
from mediaman.services.downloads.download_queue.items import build_unmatched_arr_item
from tests.helpers.factories import insert_settings

_START = datetime(2026, 1, 10, 12, tzinfo=UTC)
_DAY = timedelta(days=1)
_LOOK = timedelta(hours=6)


def _movie(**overrides):
    return {
        "id": 7,
        "title": "Dune",
        "year": 2021,
        "monitored": True,
        "hasFile": False,
        "isAvailable": True,
        "added": "2023-01-01T00:00:00Z",
        "digitalRelease": "2021-01-01T00:00:00Z",
        **overrides,
    }


def _show(*monitored):
    return {
        "id": 3,
        "title": "Show",
        "year": 2019,
        "monitored": True,
        "added": "2023-01-01T00:00:00Z",
        "statistics": {"episodeFileCount": 0},
        "seasons": [{"seasonNumber": n, "monitored": n in monitored} for n in (1, 2)],
    }


class Stack:
    """A Radarr and a Sonarr on a real database, with a clock that is stepped by hand."""

    def __init__(self, conn: sqlite3.Connection, monkeypatch) -> None:
        self.conn = conn
        self.now = _START
        self.radarr, self.sonarr = MagicMock(), MagicMock()
        for client in (self.radarr, self.sonarr):
            client.get_queue.return_value = []
        self.radarr.get_movies.return_value = []
        self.sonarr.get_series.return_value = []
        self.sonarr.get_episodes.return_value = [{"airDateUtc": "2020-01-01T00:00:00Z"}]
        for module in ("arr.build", "arr.auto_abandon", "downloads.abandon"):
            for name, client in (("radarr", self.radarr), ("sonarr", self.sonarr)):
                monkeypatch.setattr(
                    f"mediaman.services.{module}.build_{name}_from_db",
                    lambda *a, client=client: client,
                    raising=False,
                )
        monkeypatch.setattr(
            "mediaman.services.arr.fetcher._searching_since.now_utc",
            lambda: self.now,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.items.time.time",
            lambda: self.now.timestamp(),
        )

    def sweep(self, after: timedelta = timedelta()) -> None:
        """Keep looking for *after*, then let auto-abandon decide."""
        until = self.now + after
        while until - self.now > _LOOK:
            self.now += _LOOK
            fetch_arr_queue(self.conn, "x" * 64)
        self.now = until
        for card in fetch_arr_queue(self.conn, "x" * 64):
            maybe_auto_abandon(self.conn, "x" * 64, item=card, now=self.now.timestamp())

    def abandon_offered(self) -> bool:
        (card,) = fetch_arr_queue(self.conn, "x" * 64)
        return build_unmatched_arr_item(card, {}, lambda *a, **k: "", lambda *a: "")[
            "abandon_visible"
        ]


@pytest.fixture
def stack(tmp_path, monkeypatch):
    reset_search_triggers()
    conn = init_db(str(tmp_path / "mediaman.db"))
    insert_settings(conn, auto_abandon_enabled="true")
    yield Stack(conn, monkeypatch)
    conn.close()


def test_movie_re_monitored_years_after_it_was_added_is_not_abandoned(stack):
    stack.radarr.get_movies.return_value = [_movie()]
    stack.sweep()
    assert not stack.abandon_offered()
    stack.sweep(13 * _DAY)
    stack.radarr.unmonitor_movie.assert_not_called()


def test_movie_searching_for_over_fourteen_days_is_abandoned(stack):
    stack.radarr.get_movies.return_value = [_movie()]
    stack.sweep()
    stack.now += timedelta(hours=11)
    assert stack.abandon_offered()
    stack.sweep(14 * _DAY)
    stack.radarr.unmonitor_movie.assert_called_once_with(7)


def test_movie_re_monitored_right_after_auto_abandon_starts_over(stack):
    stack.radarr.get_movies.return_value = [_movie()]
    stack.sweep()
    stack.sweep(15 * _DAY)
    stack.sweep(timedelta(minutes=5))
    stack.radarr.unmonitor_movie.assert_called_once_with(7)


def test_failed_auto_abandon_is_tried_again_on_the_next_sweep(stack):
    stack.radarr.get_movies.return_value = [_movie()]
    stack.sweep()
    stack.radarr.unmonitor_movie.side_effect = requests.ConnectionError("down")
    stack.sweep(15 * _DAY)
    stack.sweep(timedelta(minutes=5))
    assert stack.radarr.unmonitor_movie.call_count == 2


def test_movie_that_stopped_searching_starts_over_when_it_searches_again(stack):
    stack.radarr.get_movies.return_value = [_movie()]
    stack.sweep(10 * _DAY)
    stack.radarr.get_movies.return_value = [_movie(hasFile=True)]
    stack.sweep(_LOOK)
    stack.radarr.get_movies.return_value = [_movie()]
    stack.sweep(_LOOK)
    stack.sweep(10 * _DAY)
    stack.radarr.unmonitor_movie.assert_not_called()


@pytest.mark.parametrize("call", ["get_queue", "get_movies"])
def test_radarr_outage_does_not_restart_the_clock(stack, call):
    stack.radarr.get_movies.return_value = [_movie()]
    stack.sweep()
    getattr(stack.radarr, call).side_effect = requests.ConnectionError("down")
    stack.sweep(2 * _LOOK)
    getattr(stack.radarr, call).side_effect = None
    stack.sweep(14 * _DAY)
    stack.radarr.unmonitor_movie.assert_called_once_with(7)


@pytest.mark.parametrize(
    ("unseen_for", "abandoned"), [(timedelta(hours=23, minutes=59), True), (_DAY, False)]
)
def test_a_search_not_looked_at_for_a_day_starts_over(stack, unseen_for, abandoned):
    stack.radarr.get_movies.return_value = [_movie()]
    stack.sweep(10 * _DAY)
    stack.now += unseen_for
    stack.sweep()
    stack.sweep(5 * _DAY)
    assert stack.radarr.unmonitor_movie.called is abandoned
    if not abandoned:
        stack.sweep(9 * _DAY)
        stack.radarr.unmonitor_movie.assert_called_once_with(7)


def test_a_clock_kept_before_last_seen_was_recorded_goes_on_and_is_watched_from_then(stack):
    stack.conn.execute(
        "INSERT INTO arr_searching_since (key, since) VALUES ('radarr:7', ?)",
        ((_START - 20 * _DAY).isoformat(),),
    )
    stack.radarr.get_movies.return_value = [_movie()]
    stack.conn.execute("DELETE FROM settings WHERE key='auto_abandon_enabled'")
    stack.sweep()
    assert stack.abandon_offered()
    stack.now += _DAY
    assert not stack.abandon_offered()


def test_a_look_a_moment_after_the_last_writes_nothing(stack):
    stack.radarr.get_movies.return_value = [_movie()]
    stack.sweep()
    before = stack.conn.total_changes
    stack.sweep(timedelta(seconds=2))
    assert stack.conn.total_changes == before


def test_show_re_monitored_years_after_it_was_added_is_not_abandoned(stack):
    stack.sonarr.get_series.return_value = [_show(1)]
    stack.sonarr.get_series_by_id.return_value = _show(1)
    stack.sweep()
    stack.sweep(13 * _DAY)
    stack.sonarr.unmonitor_season.assert_not_called()
    stack.sweep(2 * _DAY)
    stack.sonarr.unmonitor_season.assert_called_once_with(3, 1)


def test_season_re_monitored_on_a_searching_show_restarts_its_clock(stack):
    stack.sonarr.get_series.return_value = [_show(1)]
    stack.sweep(10 * _DAY)
    stack.sonarr.get_series.return_value = [_show(1, 2)]
    stack.sonarr.get_series_by_id.return_value = _show(1, 2)
    stack.sweep()
    stack.sweep(5 * _DAY)
    stack.sonarr.unmonitor_season.assert_not_called()


def test_title_already_searching_at_an_upgrade_starts_its_clock_then(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    old = init_db(path)
    insert_settings(old, auto_abandon_enabled="true")
    old.execute("DROP TABLE arr_searching_since")
    old.execute(
        "INSERT INTO arr_search_throttle (key, last_triggered_at, search_count)"
        " VALUES ('radarr:Dune', '2025-12-31T00:00:00+00:00', 40)"
    )
    old.commit()
    old.close()

    init_db(path).close()
    conn = init_db(path)
    stack = Stack(conn, monkeypatch)
    stack.radarr.get_movies.return_value = [_movie()]
    stack.sweep()
    stack.sweep(13 * _DAY)
    stack.radarr.unmonitor_movie.assert_not_called()
    assert conn.execute("SELECT search_count FROM arr_search_throttle").fetchone()[0] == 40
    stack.sweep(2 * _DAY)
    stack.radarr.unmonitor_movie.assert_called_once_with(7)
    conn.close()


def test_show_re_monitored_right_after_auto_abandon_starts_over(stack):
    stack.sonarr.get_series.return_value = [_show(1)]
    stack.sonarr.get_series_by_id.return_value = _show(1)
    stack.sweep()
    stack.sweep(15 * _DAY)
    stack.sweep(timedelta(minutes=5))
    stack.sonarr.unmonitor_season.assert_called_once_with(3, 1)


def _card(added_at):
    return {"arr_id": 7, "is_upcoming": False, "added_at": added_at}


@pytest.mark.parametrize(
    ("since", "added_at"),
    [
        # A reinstalled Arr hands out its ids again, so the add date is the later one.
        (_START, _START + 5 * _DAY),
        (_START + 5 * _DAY, _START),
    ],
)
def test_searching_since_is_the_later_of_when_it_was_first_seen_and_when_it_was_added(
    stack, since, added_at
):
    stack.conn.execute(
        "INSERT INTO arr_searching_since (key, since) VALUES ('radarr:7', ?)", (since.isoformat(),)
    )
    card = _card(added_at.timestamp())

    stamp_searching_since(stack.conn, "radarr", [card], complete=True)

    assert card["searching_since"] == max(since, added_at).timestamp()


def _refuse_deletes(conn):
    conn.execute(
        "CREATE TRIGGER no_delete BEFORE DELETE ON arr_searching_since"
        " BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )


def test_a_failed_write_of_the_search_clocks_leaves_nothing_half_done_and_the_connection_free(
    stack,
):
    stack.conn.execute(
        "INSERT INTO arr_searching_since (key, since) VALUES ('radarr:9', ?)",
        (_START.isoformat(),),
    )
    stack.conn.commit()
    _refuse_deletes(stack.conn)

    stamp_searching_since(stack.conn, "radarr", [_card(0.0)], complete=True)

    assert not stack.conn.in_transaction
    keys = {r[0] for r in stack.conn.execute("SELECT key FROM arr_searching_since")}
    assert keys == {"radarr:9"}


def test_a_failed_forget_leaves_the_connection_free(stack):
    stack.conn.execute(
        "INSERT INTO arr_searching_since (key, since) VALUES ('radarr:7', ?)",
        (_START.isoformat(),),
    )
    stack.conn.commit()
    _refuse_deletes(stack.conn)

    forget_searching_since(stack.conn, "radarr", 7)

    assert not stack.conn.in_transaction
