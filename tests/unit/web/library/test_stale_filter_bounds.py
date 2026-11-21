"""The Library page's stale filter holds the stored day settings to their allowed range."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.db import init_db, set_connection
from mediaman.web.routes.library import fetch_library
from tests.helpers.factories import insert_media_item
from tests.unit.scanner.test_runner import _set_setting


def _movie(conn, media_id: str, *, added_days_ago: float, watched_days_ago: float | None) -> None:
    now = datetime.now(UTC)
    watched = None if watched_days_ago is None else now - timedelta(days=watched_days_ago)
    insert_media_item(
        conn,
        id=media_id,
        title=media_id,
        plex_rating_key=f"rk-{media_id}",
        added_at=(now - timedelta(days=added_days_ago)).isoformat(),
        last_watched_at=watched.isoformat() if watched else None,
        file_path="/f",
        file_size_bytes=1,
    )


@pytest.mark.parametrize(
    ("key", "added_days_ago", "watched_days_ago"),
    [
        # Added half a day ago and never watched: under a one-day minimum age it is too new.
        ("min_age_days", 0.5, None),
        # Watched half a day ago: under a one-day inactivity window it is still active.
        ("inactivity_days", 60, 0.5),
    ],
)
def test_a_stored_zero_day_setting_is_treated_as_one_day(
    db_path, key, added_days_ago, watched_days_ago
):
    conn = init_db(str(db_path))
    set_connection(conn)
    _movie(conn, "m1", added_days_ago=added_days_ago, watched_days_ago=watched_days_ago)
    _set_setting(conn, key, 0)

    _items, total = fetch_library(conn, media_type="stale")

    assert total == 0


def test_a_stored_huge_day_setting_is_treated_as_the_upper_bound(db_path):
    conn = init_db(str(db_path))
    set_connection(conn)
    _movie(conn, "old", added_days_ago=400, watched_days_ago=None)
    _set_setting(conn, "min_age_days", 10**12)

    items, total = fetch_library(conn, media_type="stale")

    assert total == 1 and items[0]["title"] == "old"
