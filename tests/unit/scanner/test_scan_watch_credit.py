"""The cleanup scan counts plays in mediaman like Plex watches."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.core.time import parse_iso_utc
from mediaman.scanner._scan_library import scan_tv_library
from mediaman.scanner.arr_dates import ArrDateCache
from mediaman.scanner.fetch import FetchedLibrary, PlexItemFetch
from mediaman.scanner.repository import last_credited_by_media_item
from mediaman.web.auth.password_hash import delete_user
from mediaman.web.repository.watch_progress import save_progress
from tests.helpers.factories import insert_media_item, insert_scheduled_action

SEASON = "season-1"


class _StubFetcher:
    def __init__(self, fetched: list[PlexItemFetch]):
        self._fetched = fetched

    def fetch_library_items(self, library_id: str) -> FetchedLibrary:
        return FetchedLibrary(items=self._fetched, skipped_keys=frozenset())


class _StubEngine:
    """The engine attributes the per-library scan reads; dry-run counts without writing."""

    def __init__(self, conn, fetched: list[PlexItemFetch]):
        self._conn = conn
        self._fetcher = _StubFetcher(fetched)
        self._arr_cache = ArrDateCache()
        self._listed_keys = set()
        self._skipped_places = set()
        self._dry_run = True
        self._grace_days = 14
        self._secret_key = "0123456789abcdef" * 4
        self._min_age_days = 30
        self._inactivity_days = 30

    def _resolve_added_at(self, item: dict) -> datetime:
        return parse_iso_utc(str(item["added_at"])) or datetime.now(UTC)


def _old_unwatched_season() -> PlexItemFetch:
    """A season added a year ago that nobody has watched in Plex."""
    return PlexItemFetch(
        item={
            "plex_rating_key": SEASON,
            "title": "Season 1",
            "show_title": "Show",
            "season_number": 1,
            "show_rating_key": "show-1",
            "added_at": (datetime.now(UTC) - timedelta(days=365)).isoformat(),
            "file_path": "/media/tv/Show/Season 1",
            "file_size_bytes": 1_000_000,
        },
        library_id="2",
        media_type="season",
        watch_history=[],
    )


def _credit_episode_play(conn, *, days_ago: int) -> datetime:
    """Record a full mediaman play of an episode of SEASON *days_ago* days ago."""
    when = datetime.now(UTC) - timedelta(days=days_ago)
    save_progress(
        conn,
        username="aoife",
        rating_key="episode-3",
        media_item_id=SEASON,
        show_rating_key="show-1",
        position_s=1500.0,
        duration_s=1500.0,
        played_s=1500.0,
        now=when,
    )
    return when


def _scan(conn) -> dict[str, int]:
    summary = {"scanned": 0, "scheduled": 0, "skipped": 0, "errors": 0}
    scan_tv_library(_StubEngine(conn, [_old_unwatched_season()]), "2", summary)()
    return summary


@pytest.fixture
def viewer(conn):
    # A plain row rather than a real account: hashing a password is slow.
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, role)"
        " VALUES ('aoife', '', '2026-01-01T00:00:00+00:00', 'viewer')"
    )
    conn.commit()
    return conn


def test_a_season_played_in_mediaman_yesterday_is_not_scheduled(viewer):
    _credit_episode_play(viewer, days_ago=1)

    summary = _scan(viewer)

    assert (summary["scheduled"], summary["skipped"]) == (0, 1)


def test_a_season_stays_safe_after_its_only_viewer_is_deleted(viewer):
    _credit_episode_play(viewer, days_ago=1)
    _scan(viewer)
    viewer.execute("DELETE FROM admin_users WHERE username = 'aoife'")
    assert last_credited_by_media_item(viewer, [SEASON]) == {}

    assert _scan(viewer)["scheduled"] == 0


def _delete_viewer(conn) -> None:
    [user_id] = conn.execute("SELECT id FROM admin_users WHERE username = 'aoife'").fetchone()
    assert delete_user(conn, user_id, current_username="admin") is True


def test_a_season_played_then_its_viewer_deleted_before_any_scan_is_not_scheduled(viewer):
    insert_media_item(viewer, id=SEASON, plex_rating_key=SEASON, media_type="season")
    _credit_episode_play(viewer, days_ago=1)
    _delete_viewer(viewer)

    assert _scan(viewer)["scheduled"] == 0


def test_deleting_a_viewer_never_moves_a_later_last_watched_back(viewer):
    later = datetime.now(UTC) - timedelta(hours=1)
    insert_media_item(
        viewer, id=SEASON, plex_rating_key=SEASON, media_type="season", last_watched_at=later
    )
    _credit_episode_play(viewer, days_ago=1)
    _delete_viewer(viewer)

    row = viewer.execute(
        "SELECT last_watched_at FROM media_items WHERE id = ?", (SEASON,)
    ).fetchone()
    assert parse_iso_utc(row["last_watched_at"]) == later


def test_a_mediaman_play_older_than_the_inactivity_window_still_allows_scheduling(viewer):
    _credit_episode_play(viewer, days_ago=45)

    assert _scan(viewer)["scheduled"] == 1


def test_a_mediaman_play_sets_the_seasons_last_watched(viewer):
    played = _credit_episode_play(viewer, days_ago=1)

    _scan(viewer)

    row = viewer.execute(
        "SELECT last_watched_at FROM media_items WHERE id = ?", (SEASON,)
    ).fetchone()
    assert parse_iso_utc(row["last_watched_at"]) == played


def test_an_already_scheduled_season_stays_scheduled_when_played(viewer):
    insert_media_item(viewer, id=SEASON, plex_rating_key=SEASON, media_type="season")
    action_id = insert_scheduled_action(viewer, media_item_id=SEASON)
    _credit_episode_play(viewer, days_ago=1)

    summary = _scan(viewer)

    rows = viewer.execute(
        "SELECT id, action FROM scheduled_actions WHERE media_item_id = ?", (SEASON,)
    ).fetchall()
    assert [(r["id"], r["action"]) for r in rows] == [(action_id, "scheduled_deletion")]
    assert summary["skipped"] == 1


def test_credit_is_the_latest_across_people_and_episodes_of_a_season(viewer):
    viewer.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, role)"
        " VALUES ('niamh', '', '2026-01-01T00:00:00+00:00', 'viewer')"
    )
    older = _credit_episode_play(viewer, days_ago=3)
    later = datetime.now(UTC) - timedelta(days=1)
    for username, rating_key, when in (
        ("niamh", "episode-1", older),
        ("aoife", "episode-4", later),
    ):
        save_progress(
            viewer,
            username=username,
            rating_key=rating_key,
            media_item_id=SEASON,
            show_rating_key="show-1",
            position_s=1500.0,
            duration_s=1500.0,
            played_s=1500.0,
            now=when,
        )

    assert last_credited_by_media_item(viewer, [SEASON]) == {SEASON: later}


def test_a_short_play_is_not_a_credit(viewer):
    save_progress(
        viewer,
        username="aoife",
        rating_key="episode-3",
        media_item_id=SEASON,
        show_rating_key="show-1",
        position_s=100.0,
        duration_s=1500.0,
        played_s=20.0,
        now=datetime.now(UTC),
    )

    assert last_credited_by_media_item(viewer, [SEASON]) == {}


def test_credit_is_only_read_for_the_asked_media_items(viewer):
    _credit_episode_play(viewer, days_ago=1)

    assert last_credited_by_media_item(viewer, ["some-other-item"]) == {}
    assert last_credited_by_media_item(viewer, []) == {}
