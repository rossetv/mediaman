"""The library sync counts plays in mediaman like Plex watches."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from mediaman.core.time import parse_iso_utc
from mediaman.scanner.engine import ScanEngine
from mediaman.web.repository.watch_progress import save_progress
from tests.helpers.factories import insert_media_item

MOVIE = "100"


@pytest.fixture
def viewer(conn):
    # A plain row rather than a real account: hashing a password is slow.
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, role)"
        " VALUES ('aoife', '', '2026-01-01T00:00:00+00:00', 'viewer')"
    )
    conn.commit()
    return conn


def _play(conn, when: datetime) -> None:
    save_progress(
        conn,
        username="aoife",
        rating_key=MOVIE,
        media_item_id=MOVIE,
        show_rating_key=None,
        position_s=6000.0,
        duration_s=6000.0,
        played_s=6000.0,
        now=when,
    )


def _sync(conn) -> dict[str, int]:
    plex = MagicMock()
    plex.get_movie_items.return_value = [
        {
            "plex_rating_key": MOVIE,
            "title": "Movie",
            "added_at": datetime.now(UTC) - timedelta(days=365),
            "file_path": "/media/movies/Movie",
            "file_size_bytes": 1_000_000,
            "poster_path": None,
        }
    ]
    plex.get_watch_history.return_value = []
    engine = ScanEngine(
        conn=conn,
        plex_client=plex,
        library_ids=["1"],
        library_types={"1": "movie"},
        secret_key="k",
    )
    return engine.sync_library()


def _last_watched(conn) -> datetime | None:
    row = conn.execute("SELECT last_watched_at FROM media_items WHERE id = ?", (MOVIE,)).fetchone()
    return parse_iso_utc(row["last_watched_at"]) if row["last_watched_at"] else None


def test_sync_sets_last_watched_from_a_mediaman_play(viewer):
    played = datetime.now(UTC) - timedelta(days=2)
    _play(viewer, played)

    summary = _sync(viewer)

    assert summary["errors"] == 0
    assert _last_watched(viewer) == played


def test_sync_never_moves_last_watched_back_to_an_older_mediaman_play(viewer):
    recent = datetime.now(UTC) - timedelta(days=1)
    insert_media_item(viewer, id=MOVIE, plex_rating_key=MOVIE, last_watched_at=recent)
    _play(viewer, recent - timedelta(days=10))

    _sync(viewer)

    assert _last_watched(viewer) == recent


def test_sync_without_a_mediaman_play_leaves_last_watched_empty(viewer):
    _sync(viewer)

    assert _last_watched(viewer) is None
