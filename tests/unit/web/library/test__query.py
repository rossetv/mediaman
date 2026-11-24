"""Tests for mediaman.web.routes.library._query.

Direct unit tests against fetch_library, fetch_stats, and the private
helper functions (days_ago, type_css, protection_label). No HTTP
client — these are pure DB-query helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from mediaman.core.format import format_bytes
from mediaman.db import init_db, set_connection
from mediaman.scanner.repository import fetch_kept_show_keys, fetch_protected_media_ids
from mediaman.web.routes.library import (
    fetch_library,
    fetch_stats,
    protection_label,
    type_css,
)
from tests.helpers.factories import insert_kept_show, insert_media_item, insert_scheduled_action


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _insert_movie(
    conn,
    media_id: str,
    title: str = "Test Movie",
    added_at: str | None = None,
    file_size: int = 1_000_000,
) -> None:
    insert_media_item(
        conn,
        id=media_id,
        title=title,
        plex_rating_key=f"rk-{media_id}",
        added_at=added_at or _now_iso(),
        file_path="/f",
        file_size_bytes=file_size,
    )


def _long_ago() -> str:
    return (datetime.now(UTC) - timedelta(days=400)).isoformat()


def _keep_forever(conn, media_id: str) -> None:
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action="protected_forever",
        scheduled_at=_now_iso(),
        token=f"tok-{media_id}",
        token_used=False,
    )


def _snooze(conn, media_id: str, until: datetime, token: str | None = None) -> None:
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action="snoozed",
        execute_at=until.isoformat(),
        token=token or f"tok-{media_id}",
        token_used=False,
    )


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
    insert_media_item(
        conn,
        id=media_id,
        title=f"{show_title} S{season:02d}",
        media_type=media_type,
        plex_rating_key=f"rk-{media_id}",
        added_at=added_at or _now_iso(),
        file_path="/f",
        file_size_bytes=500000,
        show_title=show_title,
        show_rating_key=show_rating_key,
        season_number=season,
        last_watched_at=last_watched_at,
    )


class TestTypeCss:
    def test_movie_returns_type_mov(self):
        assert type_css("movie") == "type-mov"

    def test_tv_season_returns_type_tv(self):
        assert type_css("tv_season") == "type-tv"

    def test_season_returns_type_tv(self):
        assert type_css("season") == "type-tv"

    def test_tv_returns_type_tv(self):
        assert type_css("tv") == "type-tv"

    def test_anime_returns_type_anime(self):
        assert type_css("anime") == "type-anime"

    def test_anime_season_returns_type_anime(self):
        assert type_css("anime_season") == "type-anime"

    def test_unknown_type_returns_type_mov(self):
        assert type_css("unknown") == "type-mov"


class TestProtectionLabel:
    def test_none_action_returns_none(self):
        assert protection_label(None, None) is None

    def test_protected_forever_returns_label(self):
        label = protection_label("protected_forever", None)
        assert label == "Kept forever"

    def test_snoozed_future_date_returns_label(self):
        future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        label = protection_label("snoozed", future)
        assert label is not None
        assert "10" in label or "day" in label

    def test_snoozed_past_date_returns_none(self):
        """An expired snooze is not shown as protected."""
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        label = protection_label("snoozed", past)
        assert label is None

    def test_snoozed_no_execute_at_returns_none(self):
        assert protection_label("snoozed", None) is None

    def test_snoozed_invalid_date_returns_none(self):
        assert protection_label("snoozed", "not-a-date") is None

    def test_1_day_remaining_is_singular(self):
        future = (datetime.now(UTC) + timedelta(hours=25)).isoformat()
        label = protection_label("snoozed", future)
        # Should say "1 day" (not "1 days")
        assert label is not None
        assert "days" not in label or "1 day" in label


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

    @pytest.mark.parametrize("typed", ["amelie", "oceans", "spiderman", "fast and"])
    def test_search_reads_past_accents_and_punctuation(self, db_path, typed):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Amélie & Ocean's Spider-Man: Fast & Furious")
        _insert_movie(conn, "m2", "Other")

        items, total = fetch_library(conn, q=typed)

        assert ([i["id"] for i in items], total) == (["m1"], 1)

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

    @pytest.mark.parametrize(
        "sort_key",
        [
            "added_desc",
            "added_asc",
            "name_asc",
            "name_desc",
            "size_desc",
            "size_asc",
            "watched_desc",
            "watched_asc",
        ],
    )
    def test_every_valid_sort_executes(self, db_path, sort_key):
        """Each VALID_SORTS value must produce syntactically valid SQL."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Bravo")
        _insert_movie(conn, "m2", "alpha")
        items, total = fetch_library(conn, sort=sort_key)
        assert total == 2
        assert len(items) == 2

    def test_name_asc_is_case_insensitive(self, db_path):
        """name_asc sorts case-insensitively (a, B, C — not B, C, a)."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Bravo")
        _insert_movie(conn, "m2", "alpha")
        _insert_movie(conn, "m3", "Charlie")
        items, _ = fetch_library(conn, sort="name_asc")
        assert [i["title"] for i in items] == ["alpha", "Bravo", "Charlie"]

    def test_kept_filter_only_shows_protected_items(self, db_path):
        """type=kept only returns items with an active protection row."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Protected")
        _insert_movie(conn, "m2", "Unprotected")

        now = _now_iso()
        insert_scheduled_action(
            conn,
            media_item_id="m1",
            action="protected_forever",
            scheduled_at=now,
            token="tok1",
            token_used=False,
        )

        items, total = fetch_library(conn, media_type="kept")
        assert total == 1
        assert items[0]["title"] == "Protected"

    def test_show_row_says_how_many_seasons_are_kept_when_a_later_season_is_kept(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Show", "rk1", season=1)
        _insert_tv_season(conn, "a2", "Show", "rk1", season=2)
        insert_scheduled_action(
            conn,
            media_item_id="a2",
            action="protected_forever",
            scheduled_at=_now_iso(),
            token="tok-a2",
            token_used=False,
        )
        for kwargs in ({}, {"media_type": "kept"}):
            items, _ = fetch_library(conn, **kwargs)
            assert len(items) == 1
            assert items[0]["protected"] is False
            assert items[0]["protection_label"] == "1 of 2 seasons kept"

    def test_show_row_reads_the_earliest_snooze_when_every_season_is_snoozed(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Show", "rk1", season=1)
        _insert_tv_season(conn, "a2", "Show", "rk1", season=2)
        for sid, days in (("a1", 40), ("a2", 10)):
            insert_scheduled_action(
                conn,
                media_item_id=sid,
                action="snoozed",
                execute_at=(datetime.now(UTC) + timedelta(days=days, hours=1)).isoformat(),
                token=f"tok-{sid}",
                token_used=False,
            )
        items, _ = fetch_library(conn)
        assert items[0]["protection_label"] == "Kept for 10 more days"

    def test_show_row_counts_a_lapsed_snooze_as_not_kept(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Show", "rk1", season=1)
        _insert_tv_season(conn, "a2", "Show", "rk1", season=2)
        insert_scheduled_action(
            conn,
            media_item_id="a1",
            action="protected_forever",
            scheduled_at=_now_iso(),
            token="tok-a1",
            token_used=False,
        )
        insert_scheduled_action(
            conn,
            media_item_id="a2",
            action="snoozed",
            execute_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
            token="tok-a2",
            token_used=False,
        )
        items, _ = fetch_library(conn)
        assert items[0]["protection_label"] == "1 of 2 seasons kept"

    def test_show_level_keep_protects_every_season_in_the_label(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Show", "rk1", season=1)
        _insert_tv_season(conn, "a2", "Show", "rk1", season=2)
        insert_kept_show(conn, show_rating_key="rk1", action="protected_forever")
        for kwargs in ({}, {"media_type": "kept"}):
            items, _ = fetch_library(conn, **kwargs)
            assert items[0]["protection_label"] == "Kept forever"

    def test_show_without_rating_key_says_a_later_kept_season_is_kept(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Show", None, season=1)
        _insert_tv_season(conn, "a2", "Show", None, season=2)
        insert_scheduled_action(
            conn,
            media_item_id="a2",
            action="protected_forever",
            scheduled_at=_now_iso(),
            token="tok-a2",
            token_used=False,
        )
        items, _ = fetch_library(conn)
        assert items[0]["protected"] is False
        assert items[0]["protection_label"] == "1 of 2 seasons kept"

    def test_every_season_kept_reads_the_snooze_that_lapses_before_a_forever_keep(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Show", "rk1", season=1)
        _insert_tv_season(conn, "a2", "Show", "rk1", season=2)
        future = (datetime.now(UTC) + timedelta(days=10, hours=1)).isoformat()
        insert_scheduled_action(
            conn,
            media_item_id="a1",
            action="snoozed",
            execute_at=future,
            token="tok-a1",
            token_used=False,
        )
        insert_scheduled_action(
            conn,
            media_item_id="a2",
            action="protected_forever",
            scheduled_at=_now_iso(),
            token="tok-a2",
            token_used=False,
        )
        items, _ = fetch_library(conn)
        assert items[0]["protection_label"] == "Kept for 10 more days"

    def test_stale_view_ignores_a_keep_on_a_season_left_out_of_the_row(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Show", "rk1", season=1, added_at=_long_ago())
        _insert_tv_season(conn, "a2", "Show", "rk1", season=2)
        _keep_forever(conn, "a2")
        (row,) = fetch_library(conn, media_type="stale")[0]
        assert (row["type_label"], row["protected"], row["protection_label"]) == (
            "1 season",
            False,
            None,
        )

    def test_search_ignores_a_keep_on_a_season_it_did_not_match(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Show", "rk1", season=1)
        _insert_tv_season(conn, "a2", "Show", "rk1", season=2)
        conn.execute("UPDATE media_items SET title = 'Pilot' WHERE id = 'a1'")
        _keep_forever(conn, "a2")
        (row,) = fetch_library(conn, q="Pilot")[0]
        assert (row["protected"], row["protection_label"]) == (False, None)

    def test_row_of_kept_seasons_above_the_lowest_reads_its_own_earliest_lapse(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Show", "rk1", season=1)
        _insert_tv_season(conn, "a2", "Show", "rk1", season=2, added_at=_long_ago())
        _insert_tv_season(conn, "a3", "Show", "rk1", season=3, added_at=_long_ago())
        for sid, days in (("a1", 5), ("a2", 40), ("a3", 10)):
            _snooze(conn, sid, datetime.now(UTC) + timedelta(days=days, hours=1))
        (row,) = fetch_library(conn, media_type="stale")[0]
        assert (row["type_label"], row["protected"], row["protection_label"]) == (
            "2 seasons",
            True,
            "Kept for 10 more days",
        )

    def test_show_level_keep_protects_a_row_whose_seasons_have_no_keep(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Show", "rk1", season=1)
        _insert_tv_season(conn, "a2", "Show", "rk1", season=2)
        insert_kept_show(conn, show_rating_key="rk1", action="protected_forever")
        (row,) = fetch_library(conn)[0]
        assert (row["protected"], row["protection_label"]) == (True, "Kept forever")

    def test_later_snooze_wins_whatever_utc_offset_it_is_written_in(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Dune")
        now = datetime.now(UTC)
        earlier = now + timedelta(days=10, hours=1)
        later = now + timedelta(days=11, hours=2)
        # As text the earlier instant sorts last: its +14:00 wall clock is ahead.
        _snooze(conn, "m1", earlier.astimezone(timezone(timedelta(hours=14))), token="tok-e")
        _snooze(conn, "m1", later.astimezone(timezone(timedelta(hours=-12))), token="tok-l")
        (row,) = fetch_library(conn)[0]
        assert row["protection_label"] == "Kept for 11 more days"

    def test_show_stays_unprotected_when_another_show_has_a_kept_season(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "a1", "Alpha", "rk1")
        _insert_tv_season(conn, "b1", "Beta", "rk2")
        insert_scheduled_action(
            conn,
            media_item_id="b1",
            action="protected_forever",
            scheduled_at=_now_iso(),
            token="tok-b1",
            token_used=False,
        )
        items, _ = fetch_library(conn)
        assert {i["title"]: i["protected"] for i in items} == {"Alpha": False, "Beta": True}

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


class TestArt:
    def test_a_show_row_has_the_show_s_own_art_as_browse_has(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "501", "Glass Coast", "500", season=1)
        _insert_tv_season(conn, "502", "Glass Coast", "500", season=2)
        _insert_movie(conn, "100")
        items, _ = fetch_library(conn)
        assert {i["id"]: i["plex_rating_key"] for i in items} == {"501": "500", "100": "rk-100"}

    @pytest.mark.parametrize("show_key", [None, ""])
    def test_a_show_row_without_its_key_keeps_its_first_season_s_art(self, db_path, show_key):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_tv_season(conn, "501", "Glass Coast", show_key)
        items, _ = fetch_library(conn)
        assert [i["plex_rating_key"] for i in items] == ["rk-501"]


class TestKeepsInForce:
    """The page reads a keep exactly while the scanner would honour it."""

    def test_forever_keep_made_from_an_email_reads_kept_and_is_listed(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Dune")
        insert_scheduled_action(
            conn,
            media_item_id="m1",
            action="protected_forever",
            execute_at=None,
            token="tok-m1",
            token_used=True,
        )
        items, total = fetch_library(conn, media_type="kept")
        assert total == 1
        assert (items[0]["protected"], items[0]["protection_label"]) == (True, "Kept forever")

    def test_snooze_ending_within_a_day_reads_kept(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", "Dune")
        _snooze(conn, "m1", datetime.now(UTC) + timedelta(hours=2))
        (row,) = fetch_library(conn)[0]
        assert (row["protected"], row["protection_label"]) == (True, "Kept for less than a day")

    @pytest.mark.parametrize(
        "lapsed_keep",
        ["item-snooze", "item-snooze-from-email", "show-snooze"],
    )
    def test_kept_filter_leaves_out_a_lapsed_keep(self, db_path, lapsed_keep):
        conn = init_db(str(db_path))
        set_connection(conn)
        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        _insert_tv_season(conn, "a1", "Show", "rk1")
        if lapsed_keep == "show-snooze":
            insert_kept_show(conn, show_rating_key="rk1", action="snoozed", execute_at=yesterday)
        else:
            insert_scheduled_action(
                conn,
                media_item_id="a1",
                action="snoozed",
                execute_at=yesterday,
                token="tok-a1",
                token_used=lapsed_keep == "item-snooze-from-email",
            )
        assert fetch_library(conn, media_type="kept") == ([], 0)

    def test_lapsed_show_snooze_does_not_protect_its_seasons(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        _insert_tv_season(conn, "a1", "Show", "rk1")
        insert_kept_show(conn, show_rating_key="rk1", action="snoozed", execute_at=yesterday)
        (row,) = fetch_library(conn)[0]
        assert row["protected"] is False
        assert row["protection_label"] is None

    def test_live_show_snooze_protects_its_seasons(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        _insert_tv_season(conn, "a1", "Show", "rk1")
        insert_kept_show(conn, show_rating_key="rk1", action="snoozed", execute_at=tomorrow)
        (row,) = fetch_library(conn)[0]
        assert row["protected"] is True
        assert row["protection_label"]

    def test_kept_filter_agrees_with_the_scanner_at_the_instant_a_snooze_ends(
        self, db_path, monkeypatch
    ):
        """An item snooze is still kept at its end instant; a show snooze is not."""
        conn = init_db(str(db_path))
        set_connection(conn)
        at = datetime.now(UTC).isoformat()
        monkeypatch.setattr("mediaman.web.repository.library_query._query.now_iso", lambda: at)
        _insert_movie(conn, "m1", "Item Snoozed")
        _snooze(conn, "m1", datetime.fromisoformat(at))
        _insert_tv_season(conn, "a1", "Show Snoozed", "rk1")
        insert_kept_show(conn, show_rating_key="rk1", action="snoozed", execute_at=at)

        items, total = fetch_library(conn, media_type="kept")
        assert [i["title"] for i in items] == ["Item Snoozed"]
        assert total == 1
        assert fetch_protected_media_ids(conn, ["m1", "a1"], at) == {"m1"}
        assert fetch_kept_show_keys(conn, ["rk1"], at) == set()


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

    def test_stale_count_groups_seasons_into_one_per_show(self, db_path):
        """A stale show with many seasons counts as one stale item, not N.

        Regression: ``count_stale`` counted raw rows while the type counts
        grouped seasons into shows, so a single 10-season show inflated the
        "Stale" stat past the total item count (the impossible ``266 of 138``).
        """
        conn = init_db(str(db_path))
        set_connection(conn)
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        for season in (1, 2, 3):
            _insert_tv_season(
                conn, f"s{season}", "Old Show", "rk-old", season=season, added_at=old_date
            )
        stats = fetch_stats(conn)
        assert stats["tv"] == 1
        assert stats["stale"] == 1  # three stale seasons collapse to one stale show
        assert stats["stale"] <= stats["total"]

    def test_stale_count_never_exceeds_total(self, db_path):
        """Mixed library: stale (display-item unit) is always ≤ total."""
        conn = init_db(str(db_path))
        set_connection(conn)
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        _insert_movie(conn, "m1", "Old Movie", added_at=old_date)
        for season in range(1, 6):
            _insert_tv_season(
                conn, f"s{season}", "Old Show", "rk-old", season=season, added_at=old_date
            )
        stats = fetch_stats(conn)
        assert stats["total"] == 2  # one movie + one grouped show
        assert stats["stale"] == 2  # both are stale, counted in the same unit
        assert stats["stale"] <= stats["total"]

    def test_recently_added_show_is_not_stale(self, db_path):
        """A show whose seasons are all fresh is not counted as stale."""
        conn = init_db(str(db_path))
        set_connection(conn)
        for season in (1, 2):
            _insert_tv_season(conn, f"s{season}", "Fresh Show", "rk-fresh", season=season)
        stats = fetch_stats(conn)
        assert stats["stale"] == 0


def _schedule_deletion(conn, media_id: str, days: float, **fields) -> None:
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action="scheduled_deletion",
        execute_at=(datetime.now(UTC) + timedelta(days=days)).isoformat(),
        token=f"tok-{media_id}",
        **fields,
    )


def _insert_season(conn, media_id: str, show: str, key: str, size: int = 10) -> None:
    insert_media_item(
        conn,
        id=media_id,
        title=f"{show} {media_id}",
        media_type="tv_season",
        show_title=show,
        show_rating_key=key,
        plex_rating_key=f"rk-{media_id}",
        added_at=_now_iso(),
        file_path="/f",
        file_size_bytes=size,
    )


class TestPendingDeletion:
    def test_row_carries_the_pending_deletion_date_and_days(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1")
        _insert_movie(conn, "m2")
        _schedule_deletion(conn, "m1", 5.5)
        items, _ = fetch_library(conn)
        by_id = {i["id"]: i for i in items}
        assert by_id["m1"]["deletion_at"] is not None
        assert by_id["m1"]["deletion_days"] in (5, 6)
        assert by_id["m2"]["deletion_at"] is None
        assert by_id["m2"]["deletion_days"] is None

    def test_a_deletion_past_its_date_is_still_to_come(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1")
        _schedule_deletion(conn, "m1", -2)

        items, _ = fetch_library(conn)

        assert items[0]["deletion_at"] is not None
        assert items[0]["deletion_days"] == 0

    def test_used_and_deleting_actions_do_not_count(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        for i, (days, kw) in enumerate(
            [(3, {"token_used": True}), (-2, {"delete_status": "deleting"})]
        ):
            _insert_movie(conn, f"m{i}")
            _schedule_deletion(conn, f"m{i}", days, **kw)
        items, _ = fetch_library(conn)
        assert all(i["deletion_at"] is None for i in items)

    def test_an_item_whose_library_is_not_selected_shows_no_deletion(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1")
        _insert_season(conn, "s1", "Show", "k1")
        _schedule_deletion(conn, "m1", 5.5)
        _schedule_deletion(conn, "s1", 5.5)
        conn.execute("UPDATE settings SET value = '[\"2\"]' WHERE key = 'plex_libraries'")
        items, _ = fetch_library(conn)
        assert [i["deletion_at"] for i in items] == [None, None]

    def test_show_row_takes_its_soonest_season(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_season(conn, "s1", "Show", "k1")
        _insert_season(conn, "s2", "Show", "k1")
        _schedule_deletion(conn, "s1", 9.5)
        _schedule_deletion(conn, "s2", 2.5)
        items, _ = fetch_library(conn)
        assert len(items) == 1
        assert items[0]["deletion_days"] in (2, 3)

    def test_deletion_sorts_put_unscheduled_rows_last(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        for mid, days in [("a", 9.5), ("b", 2.5), ("c", None)]:
            _insert_movie(conn, mid, title=mid)
            if days is not None:
                _schedule_deletion(conn, mid, days)
        asc, _ = fetch_library(conn, sort="deletion_asc")
        desc, _ = fetch_library(conn, sort="deletion_desc")
        assert [i["id"] for i in asc] == ["b", "a", "c"]
        assert [i["id"] for i in desc] == ["a", "b", "c"]


class TestStatsSizesAndDue:
    def test_sizes_by_type(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1", file_size=2048)
        _insert_season(conn, "s1", "Show", "k1", size=1024)
        _insert_season(conn, "s2", "Show", "k1", size=1024)
        stats = fetch_stats(conn)
        assert stats["movies_size"] == format_bytes(2048)
        assert stats["tv_size"] == format_bytes(2048)
        assert stats["anime_size"] == format_bytes(0)

    def test_due_soon_counts_items_in_the_next_14_days_once_per_show(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "soon")
        _insert_movie(conn, "later")
        _insert_season(conn, "s1", "Show", "k1")
        _insert_season(conn, "s2", "Show", "k1")
        _insert_movie(conn, "past")
        _insert_movie(conn, "begun")
        _schedule_deletion(conn, "past", -1)
        _schedule_deletion(conn, "begun", -1, delete_status="deleting")
        _schedule_deletion(conn, "soon", 3)
        _schedule_deletion(conn, "later", 20)
        _schedule_deletion(conn, "s1", 2)
        _schedule_deletion(conn, "s2", 4)
        stats = fetch_stats(conn)
        assert stats["due_soon"] == 3
        assert stats["due_soon_days"] == 14

    def test_due_soon_leaves_out_items_whose_library_is_not_selected(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m1")
        _insert_season(conn, "s1", "Show", "k1")
        _schedule_deletion(conn, "m1", 3)
        _schedule_deletion(conn, "s1", 3)
        conn.execute("UPDATE settings SET value = '[\"2\"]' WHERE key = 'plex_libraries'")
        assert fetch_stats(conn)["due_soon"] == 0
