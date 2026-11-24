"""Marks in Watch (watched, not watched, dismissed) change nothing the cleanup scan decides."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.core.time import parse_iso_utc
from mediaman.web.repository.watch_progress import (
    MarkTarget,
    dismiss,
    mark_watched,
    restore_marks,
    save_progress,
    unmark_watched,
)
from tests.unit.scanner.test_scan_watch_credit import SEASON, _delete_viewer, _scan, viewer

__all__ = ["viewer"]

NOW = datetime.now(UTC)
EPISODES = [MarkTarget(f"episode-{n}", SEASON, "show-1", 1500.0) for n in range(1, 6)]


def _mark(conn):
    return mark_watched(conn, "aoife", EPISODES, NOW)


def _unmark(conn):
    return unmark_watched(conn, "aoife", "season", SEASON)


def _nothing(conn):
    pass


def _mark_and_undo(conn):
    restore_marks(conn, "aoife", _mark(conn))


def _mark_and_unmark(conn):
    _mark(conn)
    _unmark(conn)


def _unmark_and_undo(conn):
    restore_marks(conn, "aoife", _unmark(conn))


def _unmark_then_mark(conn):
    _unmark(conn)
    _mark(conn)


def _dismiss(conn):
    dismiss(conn, "aoife", "show-1", NOW)


def _play(
    conn,
    days_ago: int,
    *,
    played: float,
    position: float = 1500.0,
    media_item_id: str | None = SEASON,
):
    when = NOW - timedelta(days=days_ago)
    save_progress(
        conn,
        username="aoife",
        rating_key="episode-3",
        media_item_id=media_item_id,
        show_rating_key="show-1",
        position_s=position,
        duration_s=1500.0,
        played_s=played,
        now=when,
    )
    return when


# What was played before the mark, whether the scan then schedules the season, and whether
# that play is its last watched time.
HISTORIES = {
    "never played": (lambda conn: None, 1, False),
    "played yesterday": (lambda conn: _play(conn, 1, played=1500.0), 0, True),
    "played part way yesterday": (
        lambda conn: _play(conn, 1, played=700.0, position=700.0),
        0,
        True,
    ),
    "played long ago": (lambda conn: _play(conn, 45, played=1500.0), 1, True),
    "skipped through without credit": (lambda conn: _play(conn, 1, played=20.0), 1, False),
    "played yesterday under no season": (
        lambda conn: _play(conn, 1, played=700.0, position=700.0, media_item_id=None),
        1,
        False,
    ),
}


@pytest.mark.parametrize("history", HISTORIES)
@pytest.mark.parametrize(
    "act",
    [
        _nothing,
        _mark,
        _unmark,
        _mark_and_undo,
        _mark_and_unmark,
        _unmark_and_undo,
        _unmark_then_mark,
        _dismiss,
    ],
)
def test_the_scan_decides_the_same_with_and_without_a_mark(viewer, act, history):
    play, scheduled, is_last_watched = HISTORIES[history]
    played_at = play(viewer)

    act(viewer)
    summary = _scan(viewer)

    assert summary["scheduled"] == scheduled
    [stored] = viewer.execute(
        "SELECT last_watched_at FROM media_items WHERE id = ?", (SEASON,)
    ).fetchone()
    assert (parse_iso_utc(stored) if stored else None) == (played_at if is_last_watched else None)


def test_a_mark_is_no_watch_when_its_person_is_deleted(viewer):
    _mark(viewer)
    _delete_viewer(viewer)

    assert _scan(viewer)["scheduled"] == 1
