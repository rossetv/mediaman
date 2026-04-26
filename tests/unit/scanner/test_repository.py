"""Tests for mediaman.scanner.repository.

Covers: upsert_media_item, update_last_watched, count_items_in_libraries,
fetch_ids_in_libraries, delete_media_items, is_protected, is_already_scheduled,
has_expired_snooze, is_show_kept, read_delete_allowed_roots_setting,
cleanup_expired_snoozes.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from mediaman.db import init_db
from mediaman.scanner import repository


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _insert_item(
    conn,
    *,
    media_id="m1",
    title="Test Film",
    media_type="movie",
    library_id=1,
    plex_rating_key="m1",
    show_rating_key=None,
    file_path="/media/film.mkv",
    file_size_bytes=1_000_000,
    added_at="2020-01-01T00:00:00+00:00",
):
    conn.execute(
        "INSERT INTO media_items "
        "(id, title, media_type, plex_library_id, plex_rating_key, show_rating_key, "
        "added_at, file_path, file_size_bytes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            media_id,
            title,
            media_type,
            library_id,
            plex_rating_key,
            show_rating_key,
            added_at,
            file_path,
            file_size_bytes,
        ),
    )
    conn.commit()


def _insert_action(
    conn,
    *,
    media_id="m1",
    action="scheduled_deletion",
    token="tok",
    token_used=0,
    execute_at=None,
    delete_status="pending",
):
    conn.execute(
        "INSERT INTO scheduled_actions "
        "(media_item_id, action, scheduled_at, execute_at, token, token_used, delete_status) "
        "VALUES (?, ?, '2020-01-01', ?, ?, ?, ?)",
        (media_id, action, execute_at, token, token_used, delete_status),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# upsert_media_item
# ---------------------------------------------------------------------------


class TestUpsertMediaItem:
    def _item_dict(self, rk="m1", title="Film"):
        return {
            "plex_rating_key": rk,
            "title": title,
            "added_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "file_path": "/media/film.mkv",
            "file_size_bytes": 5_000_000,
            "poster_path": None,
        }

    def test_inserts_new_item(self, conn):
        repository.upsert_media_item(
            conn, item=self._item_dict(), library_id="1", media_type="movie", arr_date=None
        )
        row = conn.execute("SELECT title FROM media_items WHERE id='m1'").fetchone()
        assert row["title"] == "Film"

    def test_updates_existing_item_on_conflict(self, conn):
        item = self._item_dict()
        repository.upsert_media_item(
            conn, item=item, library_id="1", media_type="movie", arr_date=None
        )
        item["title"] = "Updated Film"
        repository.upsert_media_item(
            conn, item=item, library_id="1", media_type="movie", arr_date=None
        )
        rows = conn.execute("SELECT title FROM media_items WHERE id='m1'").fetchall()
        assert len(rows) == 1
        assert rows[0]["title"] == "Updated Film"

    def test_arr_date_preferred_over_plex_added_at(self, conn):
        item = self._item_dict()
        arr_date = "2023-06-15T12:00:00Z"
        repository.upsert_media_item(
            conn, item=item, library_id="1", media_type="movie", arr_date=arr_date
        )
        row = conn.execute("SELECT added_at FROM media_items WHERE id='m1'").fetchone()
        # The stored date must come from arr_date, not Plex's 2024-01-01.
        assert "2023-06-15" in row["added_at"]


# ---------------------------------------------------------------------------
# update_last_watched
# ---------------------------------------------------------------------------


class TestUpdateLastWatched:
    def test_stores_most_recent_watch(self, conn):
        _insert_item(conn)
        history = [
            {"viewed_at": datetime(2024, 1, 10, tzinfo=timezone.utc)},
            {"viewed_at": datetime(2024, 3, 20, tzinfo=timezone.utc)},
        ]
        repository.update_last_watched(conn, "m1", history)
        row = conn.execute("SELECT last_watched_at FROM media_items WHERE id='m1'").fetchone()
        assert "2024-03-20" in row["last_watched_at"]

    def test_empty_history_is_noop(self, conn):
        _insert_item(conn)
        repository.update_last_watched(conn, "m1", [])
        row = conn.execute("SELECT last_watched_at FROM media_items WHERE id='m1'").fetchone()
        assert row["last_watched_at"] is None


# ---------------------------------------------------------------------------
# count_items_in_libraries / fetch_ids_in_libraries
# ---------------------------------------------------------------------------


class TestCountAndFetchLibraryItems:
    def test_counts_correct_library(self, conn):
        _insert_item(conn, media_id="a1", library_id=10)
        _insert_item(conn, media_id="a2", library_id=10)
        _insert_item(conn, media_id="b1", library_id=20)
        assert repository.count_items_in_libraries(conn, [10]) == 2
        assert repository.count_items_in_libraries(conn, [20]) == 1

    def test_empty_library_ids_returns_zero(self, conn):
        _insert_item(conn)
        assert repository.count_items_in_libraries(conn, []) == 0

    def test_fetch_ids_returns_correct_ids(self, conn):
        _insert_item(conn, media_id="x1", library_id=5)
        _insert_item(conn, media_id="x2", library_id=5)
        ids = repository.fetch_ids_in_libraries(conn, [5])
        assert set(ids) == {"x1", "x2"}


# ---------------------------------------------------------------------------
# delete_media_items
# ---------------------------------------------------------------------------


class TestDeleteMediaItems:
    def test_deletes_items_and_actions(self, conn):
        _insert_item(conn)
        _insert_action(conn)
        repository.delete_media_items(conn, ["m1"])
        assert conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone() is None
        assert (
            conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id='m1'").fetchone()
            is None
        )

    def test_empty_list_is_noop(self, conn):
        _insert_item(conn)
        repository.delete_media_items(conn, [])
        assert conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone() is not None


# ---------------------------------------------------------------------------
# is_protected
# ---------------------------------------------------------------------------


class TestIsProtected:
    def test_protected_forever_returns_true(self, conn):
        _insert_item(conn)
        _insert_action(conn, action="protected_forever", token="pf-tok")
        assert repository.is_protected(conn, "m1") is True

    def test_active_snooze_returns_true(self, conn):
        _insert_item(conn)
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-tok", execute_at=future)
        assert repository.is_protected(conn, "m1") is True

    def test_expired_snooze_returns_false(self, conn):
        _insert_item(conn)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-tok", execute_at=past)
        assert repository.is_protected(conn, "m1") is False

    def test_no_action_returns_false(self, conn):
        _insert_item(conn)
        assert repository.is_protected(conn, "m1") is False


# ---------------------------------------------------------------------------
# is_already_scheduled
# ---------------------------------------------------------------------------


class TestIsAlreadyScheduled:
    def test_pending_deletion_returns_true(self, conn):
        _insert_item(conn)
        _insert_action(conn, action="scheduled_deletion", token_used=0)
        assert repository.is_already_scheduled(conn, "m1") is True

    def test_used_token_returns_false(self, conn):
        _insert_item(conn)
        _insert_action(conn, action="scheduled_deletion", token_used=1)
        assert repository.is_already_scheduled(conn, "m1") is False

    def test_no_action_returns_false(self, conn):
        _insert_item(conn)
        assert repository.is_already_scheduled(conn, "m1") is False


# ---------------------------------------------------------------------------
# has_expired_snooze
# ---------------------------------------------------------------------------


class TestHasExpiredSnooze:
    def test_consumed_snooze_returns_true(self, conn):
        _insert_item(conn)
        _insert_action(conn, action="snoozed", token="sn", token_used=1)
        assert repository.has_expired_snooze(conn, "m1") is True

    def test_active_snooze_returns_false(self, conn):
        _insert_item(conn)
        _insert_action(conn, action="snoozed", token="sn", token_used=0)
        assert repository.has_expired_snooze(conn, "m1") is False

    def test_no_snooze_returns_false(self, conn):
        _insert_item(conn)
        assert repository.has_expired_snooze(conn, "m1") is False


# ---------------------------------------------------------------------------
# is_show_kept
# ---------------------------------------------------------------------------


class TestIsShowKept:
    def test_none_rating_key_returns_false(self, conn):
        assert repository.is_show_kept(conn, None) is False

    def test_protected_forever_returns_true(self, conn):
        conn.execute(
            "INSERT INTO kept_shows (show_rating_key, show_title, action, created_at) "
            "VALUES ('rk1', 'Show', 'protected_forever', '2026-01-01')"
        )
        conn.commit()
        assert repository.is_show_kept(conn, "rk1") is True

    def test_active_snooze_returns_true(self, conn):
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        conn.execute(
            "INSERT INTO kept_shows (show_rating_key, show_title, action, execute_at, created_at) "
            "VALUES ('rk2', 'Show', 'snoozed', ?, '2026-01-01')",
            (future,),
        )
        conn.commit()
        assert repository.is_show_kept(conn, "rk2") is True

    def test_expired_snooze_returns_false_and_cleans_up(self, conn):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        conn.execute(
            "INSERT INTO kept_shows (show_rating_key, show_title, action, execute_at, created_at) "
            "VALUES ('rk3', 'Show', 'snoozed', ?, '2026-01-01')",
            (past,),
        )
        conn.commit()
        result = repository.is_show_kept(conn, "rk3")
        assert result is False
        # Expired row should have been cleaned up.
        assert (
            conn.execute("SELECT id FROM kept_shows WHERE show_rating_key='rk3'").fetchone() is None
        )

    def test_unknown_show_returns_false(self, conn):
        assert repository.is_show_kept(conn, "unknown-rk") is False


# ---------------------------------------------------------------------------
# read_delete_allowed_roots_setting
# ---------------------------------------------------------------------------


class TestReadDeleteAllowedRoots:
    def test_reads_json_from_db(self, conn, monkeypatch):
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('delete_allowed_roots', ?, 0, '2026-01-01')",
            (json.dumps(["/media/movies", "/media/tv"]),),
        )
        conn.commit()
        roots = repository.read_delete_allowed_roots_setting(conn)
        assert roots == ["/media/movies", "/media/tv"]

    def test_reads_colon_separated_env_var(self, conn, monkeypatch):
        conn.execute("DELETE FROM settings WHERE key='delete_allowed_roots'")
        conn.commit()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/media/movies:/media/tv")
        roots = repository.read_delete_allowed_roots_setting(conn)
        assert roots == ["/media/movies", "/media/tv"]

    def test_empty_when_nothing_configured(self, conn, monkeypatch):
        conn.execute("DELETE FROM settings WHERE key='delete_allowed_roots'")
        conn.commit()
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        roots = repository.read_delete_allowed_roots_setting(conn)
        assert roots == []


# ---------------------------------------------------------------------------
# cleanup_expired_snoozes
# ---------------------------------------------------------------------------


class TestCleanupExpiredSnoozes:
    def test_removes_past_snoozes(self, conn):
        _insert_item(conn)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-exp", execute_at=past)
        repository.cleanup_expired_snoozes(conn, datetime.now(timezone.utc).isoformat())
        conn.commit()
        assert (
            conn.execute("SELECT id FROM scheduled_actions WHERE token='sn-exp'").fetchone() is None
        )

    def test_keeps_future_snoozes(self, conn):
        _insert_item(conn)
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-fut", execute_at=future)
        repository.cleanup_expired_snoozes(conn, datetime.now(timezone.utc).isoformat())
        conn.commit()
        assert (
            conn.execute("SELECT id FROM scheduled_actions WHERE token='sn-fut'").fetchone()
            is not None
        )
