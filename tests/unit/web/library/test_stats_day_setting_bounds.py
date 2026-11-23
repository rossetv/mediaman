"""The Library stats bar holds the stored day settings to their allowed range."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mediaman.db import init_db, set_connection
from mediaman.web.repository.library_query import fetch_stats
from tests.helpers.factories import insert_media_item
from tests.unit.scanner.test_runner import _set_setting


def _movie(conn, media_id: str, *, added_days_ago: float) -> None:
    now = datetime.now(UTC)
    insert_media_item(
        conn,
        id=media_id,
        title=media_id,
        plex_rating_key=f"rk-{media_id}",
        added_at=(now - timedelta(days=added_days_ago)).isoformat(),
        last_watched_at=None,
        file_path="/f",
        file_size_bytes=1,
    )


def test_a_stored_huge_day_setting_does_not_overflow_the_stale_count(db_path):
    conn = init_db(str(db_path))
    set_connection(conn)
    _movie(conn, "old", added_days_ago=400)
    _set_setting(conn, "min_age_days", 10**12)
    _set_setting(conn, "inactivity_days", 10**12)

    assert fetch_stats(conn)["stale"] == 1


def test_a_stored_zero_min_age_is_treated_as_one_day(db_path):
    conn = init_db(str(db_path))
    set_connection(conn)
    _movie(conn, "new", added_days_ago=0.5)
    _set_setting(conn, "min_age_days", 0)

    stats = fetch_stats(conn)

    assert stats["stale"] == 0 and stats["stale_min_age"] == 1
