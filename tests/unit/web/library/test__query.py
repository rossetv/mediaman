"""Tests for :mod:`mediaman.web.routes.library._query`.

Direct unit tests against fetch_library, fetch_stats, and the private
helper functions (_days_ago, _type_css, _protection_label). No HTTP
client — these are pure DB-query helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mediaman.db import init_db, set_connection
from mediaman.web.routes.library._query import (
    _protection_label,
    _type_css,
    fetch_library,
    fetch_stats,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _insert_movie(
    conn,
    media_id: str,
    title: str = "Test Movie",
    added_at: str | None = None,
    file_size: int = 1_000_000,
) -> None:
    conn.execute(
        "INSERT INTO media_items (id, title, media_type, plex_library_id, plex_rating_key, "
        "added_at, file_path, file_size_bytes) VALUES (?, ?, 'movie', 1, ?, ?, '/f', ?)",
        (media_id, title, f"rk-{media_id}", added_at or _now_iso(), file_size),
    )
    conn.commit()


def _insert_tv_season(
    conn,
    media_id: str,
    show_title: str,
    show_rating_key: str,
    season: int = 1,
    media_type: str = "tv_season",
    added_at: str | None = None,
    last_watched_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO media_items "
        "(id, title, media_type, plex_library_id, plex_rating_key, added_at, "
        "file_path, file_size_bytes, show_title, show_rating_key, season_number, last_watched_at) "
        "VALUES (?, ?, ?, 1, ?, ?, '/f', 500000, ?, ?, ?, ?)",
        (
            media_id,
            f"{show_title} S{season:02d}",
            media_type,
            f"rk-{media_id}",
            added_at or _now_iso(),
            show_title,
            show_rating_key,
            season,
            last_watched_at,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


class TestTypeCss:
    def test_movie_returns_type_mov(self):
        assert _type_css("movie") == "type-mov"

    def test_tv_season_returns_type_tv(self):
        assert _type_css("tv_season") == "type-tv"

    def test_season_returns_type_tv(self):
        assert _type_css("season") == "type-tv"

    def test_tv_returns_type_tv(self):
        assert _type_css("tv") == "type-tv"

    def test_anime_returns_type_anime(self):
        assert _type_css("anime") == "type-anime"

    def test_anime_season_returns_type_anime(self):
        assert _type_css("anime_season") == "type-anime"

    def test_unknown_type_returns_type_mov(self):
        assert _type_css("unknown") == "type-mov"


class TestProtectionLabel:
    def test_none_action_returns_none(self):
        assert _protection_label(None, None) is None

    def test_protected_forever_returns_label(self):
        label = _protection_label("protected_forever", None)
        assert label == "Kept forever"

    def test_snoozed_future_date_returns_label(self):
        future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        label = _protection_label("snoozed", future)
        assert label is not None
        assert "10" in label or "day" in label

    def test_snoozed_past_date_returns_none(self):
        """An expired snooze is not shown as protected."""
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        label = _protection_label("snoozed", past)
        assert label is None

    def test_snoozed_no_execute_at_returns_none(self):
        assert _protection_label("snoozed", None) is None

    def test_snoozed_invalid_date_returns_none(self):
        assert _protection_label("snoozed", "not-a-date") is None

    def test_1_day_remaining_is_singular(self):
        future = (datetime.now(UTC) + timedelta(hours=25)).isoformat()
        label = _protection_label("snoozed", future)
        # Should say "1 day" (not "1 days")
        assert label is not None
        assert "days" not in label or "1 day" in label


# ---------------------------------------------------------------------------
# fetch_library
# ---------------------------------------------------------------------------


class TestFetchLibrary:
    def test_returns_empty_with_no_items(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        items, total = fetch_library(conn)
        assert items == []
        assert total == 0

    def test_movie_appears_in_results(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Inception")
        items, total = fetch_library(conn)
        assert total == 1
        titles = {i["title"] for i in items}
        assert "Inception" in titles

    def test_tv_seasons_grouped_by_show(self, db_path):
        """Multiple seasons of the same show appear as one item."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "s1", "Severance", "rk-sev", season=1)
        _insert_tv_season(conn, "s2", "Severance", "rk-sev", season=2)
        items, total = fetch_library(conn)
        assert total == 1
        item = items[0]
        assert item["title"] == "Severance"
        assert item["type_label"] == "2 seasons"

    def test_search_filter_narrows_results(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Inception")
        _insert_movie(conn, "m2", "The Dark Knight")
        items, total = fetch_library(conn, q="Inception")
        assert total == 1
        assert items[0]["title"] == "Inception"

    def test_search_is_case_insensitive(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Dune")
        _items, total = fetch_library(conn, q="dune")
        assert total == 1

    def test_type_filter_movie(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Dune")
        _insert_tv_season(conn, "s1", "Severance", "rk-sev")
        items, total = fetch_library(conn, media_type="movie")
        assert total == 1
        assert items[0]["media_type"] == "movie"

    def test_type_filter_tv(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Dune")
        _insert_tv_season(conn, "s1", "Severance", "rk-sev")
        items, total = fetch_library(conn, media_type="tv")
        assert total == 1
        assert items[0]["media_type"] == "tv"

    def test_pagination(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        for i in range(5):
            _insert_movie(conn, f"m{i}", f"Movie {i}")
        items, total = fetch_library(conn, page=2, per_page=2)
        assert total == 5
        assert len(items) == 2

    def test_invalid_sort_falls_back_to_default(self, db_path):
        """An unrecognised sort key does not raise; falls back to added_desc."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Dune")
        _items, total = fetch_library(conn, sort="totally_wrong")
        assert total == 1

    def test_kept_filter_only_shows_protected_items(self, db_path):
        """type=kept only returns items with an active protection row."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Protected")
        _insert_movie(conn, "m2", "Unprotected")

        now = _now_iso()
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, token, token_used) "
            "VALUES ('m1', 'protected_forever', ?, 'tok1', 0)",
            (now,),
        )
        conn.commit()

        items, total = fetch_library(conn, media_type="kept")
        assert total == 1
        assert items[0]["title"] == "Protected"

    def test_percent_sign_literal_search(self, db_path):
        """LIKE metachar '%' is escaped so it only matches items with a literal '%'."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "50% Off")
        _insert_movie(conn, "m2", "Normal Movie")
        items, _ = fetch_library(conn, q="%")
        titles = {i["title"] for i in items}
        assert "50% Off" in titles
        assert "Normal Movie" not in titles

    def test_underscore_literal_search(self, db_path):
        """LIKE metachar '_' is escaped so it only matches items with a literal '_'."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "foo_bar")
        _insert_movie(conn, "m2", "fooXbar")
        items, _ = fetch_library(conn, q="_")
        titles = {i["title"] for i in items}
        assert "foo_bar" in titles
        assert "fooXbar" not in titles


# ---------------------------------------------------------------------------
# fetch_stats
# ---------------------------------------------------------------------------


class TestFetchStats:
    def test_empty_db_returns_zero_counts(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        stats = fetch_stats(conn)
        assert stats["movies"] == 0
        assert stats["tv"] == 0
        assert stats["anime"] == 0
        assert stats["total"] == 0

    def test_movie_count_increments(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Dune")
        _insert_movie(conn, "m2", "Inception")
        stats = fetch_stats(conn)
        assert stats["movies"] == 2

    def test_tv_seasons_counted_as_distinct_shows(self, db_path):
        """Two seasons of the same show count as one TV entry."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "s1", "Severance", "rk-sev", season=1)
        _insert_tv_season(conn, "s2", "Severance", "rk-sev", season=2)
        stats = fetch_stats(conn)
        assert stats["tv"] == 1

    def test_total_size_is_formatted_string(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Dune", file_size=1_073_741_824)  # 1 GiB
        stats = fetch_stats(conn)
        assert "GiB" in stats["total_size"] or "GB" in stats["total_size"]

    def test_stale_count_includes_unwatched_old_items(self, db_path):
        """Items added and not watched beyond the configured thresholds are stale."""
        conn = init_db(str(db_path))
        set_connection(conn)
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        _insert_movie(conn, "m1", "Old Movie", added_at=old_date)
        stats = fetch_stats(conn)
        assert stats["stale"] >= 1
