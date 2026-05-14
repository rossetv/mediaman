"""Tests for mediaman.scanner.repository.

Covers: upsert_media_item, update_last_watched, count_items_in_libraries,
fetch_ids_in_libraries, delete_media_items, is_protected, is_already_scheduled,
has_expired_snooze, is_show_kept, cleanup_expired_show_snoozes,
read_delete_allowed_roots_setting, cleanup_expired_snoozes, schedule_deletion.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.db import init_db
from mediaman.scanner import repository
from mediaman.scanner.phases.upsert import schedule_deletion
from tests.helpers.factories import insert_kept_show, insert_media_item, insert_scheduled_action


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
    insert_media_item(
        conn,
        id=media_id,
        title=title,
        media_type=media_type,
        plex_library_id=library_id,
        plex_rating_key=plex_rating_key,
        show_rating_key=show_rating_key,
        added_at=added_at,
        file_path=file_path,
        file_size_bytes=file_size_bytes,
    )


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
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action=action,
        scheduled_at="2020-01-01",
        token=token,
        token_used=bool(token_used),
        execute_at=execute_at,
        delete_status=delete_status,
    )


# ---------------------------------------------------------------------------
# upsert_media_item
# ---------------------------------------------------------------------------


class TestUpsertMediaItem:
    def _item_dict(self, rk="m1", title="Film"):
        return {
            "plex_rating_key": rk,
            "title": title,
            "added_at": datetime(2024, 1, 1, tzinfo=UTC),
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
            {"viewed_at": datetime(2024, 1, 10, tzinfo=UTC)},
            {"viewed_at": datetime(2024, 3, 20, tzinfo=UTC)},
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
# delete_media_items / delete_actions_for_media_items
# ---------------------------------------------------------------------------


class TestDeleteMediaItems:
    def test_split_delete_clears_both_tables(self, conn):
        """The scanner delete phase drops ``scheduled_actions`` first then
        ``media_items``; together the two repository functions clear both
        tables. ``delete_media_items`` no longer cascades on its own —
        the foreign key requires the actions to go first.
        """
        _insert_item(conn)
        _insert_action(conn)
        repository.delete_actions_for_media_items(conn, ["m1"])
        repository.delete_media_items(conn, ["m1"])
        assert conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone() is None
        assert (
            conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id='m1'").fetchone()
            is None
        )

    def test_delete_media_items_touches_only_media_items(self, conn):
        """``delete_media_items`` issues only the ``media_items`` DELETE —
        the ``scheduled_actions`` table belongs to its own repository.
        """
        _insert_item(conn)
        # No scheduled_actions row, so the media_items DELETE has no FK to trip.
        repository.delete_media_items(conn, ["m1"])
        assert conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone() is None

    def test_delete_actions_for_media_items_leaves_media_item(self, conn):
        """``delete_actions_for_media_items`` removes only the action rows;
        the ``media_items`` row is untouched.
        """
        _insert_item(conn)
        _insert_action(conn)
        repository.delete_actions_for_media_items(conn, ["m1"])
        assert conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone() is not None
        assert (
            conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id='m1'").fetchone()
            is None
        )

    def test_empty_list_is_noop(self, conn):
        _insert_item(conn)
        repository.delete_media_items(conn, [])
        repository.delete_actions_for_media_items(conn, [])
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
        future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-tok", execute_at=future)
        assert repository.is_protected(conn, "m1") is True

    def test_expired_snooze_returns_false(self, conn):
        _insert_item(conn)
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-tok", execute_at=past)
        assert repository.is_protected(conn, "m1") is False

    def test_pending_only_returns_false(self, conn):
        _insert_item(conn)
        _insert_action(conn, action="scheduled_deletion", token="pend-tok")
        assert repository.is_protected(conn, "m1") is False

    def test_no_action_returns_false(self, conn):
        _insert_item(conn)
        assert repository.is_protected(conn, "m1") is False

    def test_unknown_media_id_returns_false(self, conn):
        # No matching row at all — must be False, not crash.
        assert repository.is_protected(conn, "does-not-exist") is False

    def test_protected_forever_wins_over_later_expired_snooze(self, conn):
        """Regression for Domain 05 finding: ``ORDER BY id DESC LIMIT 1``
        let a later expired snooze row mask an earlier protected_forever
        row. A protected_forever row is authoritative regardless of where
        it sits in id order.
        """
        _insert_item(conn)
        # Older protected_forever row (lower id).
        _insert_action(conn, action="protected_forever", token="pf-old")
        # Newer expired snooze row (higher id, would win the old query).
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-expired", execute_at=past)
        assert repository.is_protected(conn, "m1") is True

    def test_protected_forever_wins_over_later_active_snooze(self, conn):
        """A later active snooze row must not downgrade an earlier
        protected_forever — both would normally return True, but if
        anything ever changes the snooze semantics, protected_forever
        remains the authoritative answer.
        """
        _insert_item(conn)
        _insert_action(conn, action="protected_forever", token="pf-first")
        future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-active", execute_at=future)
        assert repository.is_protected(conn, "m1") is True

    def test_active_snooze_with_later_expired_snooze_returns_true(self, conn):
        """An active snooze must still register as protected even when
        a later (higher-id) snooze row has already expired.
        """
        _insert_item(conn)
        future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-active", execute_at=future)
        _insert_action(conn, action="snoozed", token="sn-expired", execute_at=past)
        assert repository.is_protected(conn, "m1") is True

    def test_only_expired_snoozes_returns_false(self, conn):
        """Multiple expired snooze rows still mean the item is not protected."""
        _insert_item(conn)
        past1 = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        past2 = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-old", execute_at=past1)
        _insert_action(conn, action="snoozed", token="sn-recent", execute_at=past2)
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
        insert_kept_show(conn, show_rating_key="rk1", show_title="Show", action="protected_forever")
        assert repository.is_show_kept(conn, "rk1") is True

    def test_active_snooze_returns_true(self, conn):
        future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        insert_kept_show(
            conn, show_rating_key="rk2", show_title="Show", action="snoozed", execute_at=future
        )
        assert repository.is_show_kept(conn, "rk2") is True

    def test_expired_snooze_returns_false_and_cleans_up(self, conn):
        """``is_show_kept`` reports ``False`` for an expired snoozed keep
        and sweeps the row away as part of the same call.

        The read and the cleanup are expressed as two separate helpers
        (``is_show_kept_pure`` and ``cleanup_expired_show_snoozes``); this
        top-level function composes them so the legacy "ask + clean" contract
        observed by the scan engine still holds.
        """
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        insert_kept_show(
            conn, show_rating_key="rk3", show_title="Show", action="snoozed", execute_at=past
        )
        result = repository.is_show_kept(conn, "rk3")
        assert result is False
        # Expired row was swept out by the wrapper.
        assert (
            conn.execute("SELECT id FROM kept_shows WHERE show_rating_key='rk3'").fetchone() is None
        )

    def test_pure_read_helper_does_not_mutate(self, conn):
        """``is_show_kept_pure`` is side-effect-free — it never touches the DB
        even when the row is an expired snooze.
        """
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        insert_kept_show(
            conn, show_rating_key="rk-pure", show_title="Show", action="snoozed", execute_at=past
        )
        result = repository.is_show_kept_pure(conn, "rk-pure")
        assert result is False
        assert (
            conn.execute("SELECT id FROM kept_shows WHERE show_rating_key='rk-pure'").fetchone()
            is not None
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

    def test_corrupt_json_logs_warning_and_returns_empty(self, conn, monkeypatch, caplog):
        """Corrupt JSON in the DB row must emit a WARNING (§6.7) and
        still return [] so the caller's fail-closed behaviour is unchanged.
        """
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('delete_allowed_roots', ?, 0, '2026-01-01')",
            ("{bad json",),
        )
        conn.commit()
        import logging

        with caplog.at_level(logging.WARNING, logger="mediaman.scanner.repository.settings"):
            roots = repository.read_delete_allowed_roots_setting(conn)

        assert roots == []
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "mediaman.scanner.repository.settings"
        ]
        assert warning_records, "Expected a WARNING from the corrupt-JSON path; none was emitted"
        assert "scanner.delete_roots.invalid_json" in warning_records[0].message


# ---------------------------------------------------------------------------
# cleanup_expired_snoozes
# ---------------------------------------------------------------------------


class TestCleanupExpiredSnoozes:
    def test_removes_past_snoozes(self, conn):
        _insert_item(conn)
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-exp", execute_at=past)
        repository.cleanup_expired_snoozes(conn, datetime.now(UTC).isoformat())
        conn.commit()
        assert (
            conn.execute("SELECT id FROM scheduled_actions WHERE token='sn-exp'").fetchone() is None
        )

    def test_keeps_future_snoozes(self, conn):
        _insert_item(conn)
        future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        _insert_action(conn, action="snoozed", token="sn-fut", execute_at=future)
        repository.cleanup_expired_snoozes(conn, datetime.now(UTC).isoformat())
        conn.commit()
        assert (
            conn.execute("SELECT id FROM scheduled_actions WHERE token='sn-fut'").fetchone()
            is not None
        )


# ---------------------------------------------------------------------------
# cleanup_expired_show_snoozes  (Domain 05 — split from is_show_kept)
# ---------------------------------------------------------------------------


class TestCleanupExpiredShowSnoozes:
    def test_removes_expired_snoozed_kept_show(self, conn):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        insert_kept_show(
            conn, show_rating_key="rk-exp", show_title="Show", action="snoozed", execute_at=past
        )
        removed = repository.cleanup_expired_show_snoozes(conn, datetime.now(UTC).isoformat())
        conn.commit()
        assert removed == 1
        assert (
            conn.execute("SELECT id FROM kept_shows WHERE show_rating_key='rk-exp'").fetchone()
            is None
        )

    def test_keeps_active_snoozed_kept_show(self, conn):
        future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        insert_kept_show(
            conn, show_rating_key="rk-fut", show_title="Show", action="snoozed", execute_at=future
        )
        removed = repository.cleanup_expired_show_snoozes(conn, datetime.now(UTC).isoformat())
        assert removed == 0
        assert (
            conn.execute("SELECT id FROM kept_shows WHERE show_rating_key='rk-fut'").fetchone()
            is not None
        )

    def test_does_not_touch_protected_forever(self, conn):
        """``protected_forever`` rows have ``execute_at IS NULL``; they must
        survive the cleanup unconditionally."""
        insert_kept_show(
            conn, show_rating_key="rk-pf", show_title="Show", action="protected_forever"
        )
        removed = repository.cleanup_expired_show_snoozes(conn, datetime.now(UTC).isoformat())
        assert removed == 0
        assert (
            conn.execute("SELECT id FROM kept_shows WHERE show_rating_key='rk-pf'").fetchone()
            is not None
        )


# ---------------------------------------------------------------------------
# update_last_watched — monotonic guard (Domain 05)
# ---------------------------------------------------------------------------


class TestUpdateLastWatchedMonotonic:
    def test_does_not_rewind_existing_timestamp(self, conn):
        """A subsequent re-scan that fetches an older watch entry must not
        drag ``last_watched_at`` backwards.
        """
        _insert_item(conn)
        recent = datetime(2024, 6, 1, tzinfo=UTC)
        old = datetime(2023, 1, 1, tzinfo=UTC)
        repository.update_last_watched(conn, "m1", [{"viewed_at": recent}])
        repository.update_last_watched(conn, "m1", [{"viewed_at": old}])
        row = conn.execute("SELECT last_watched_at FROM media_items WHERE id='m1'").fetchone()
        # Stored value must still be the recent watch — never rewound.
        assert "2024-06-01" in row["last_watched_at"]

    def test_advances_when_newer_watch_seen(self, conn):
        """A genuinely newer watch entry must overwrite the stored value."""
        _insert_item(conn)
        old = datetime(2023, 1, 1, tzinfo=UTC)
        recent = datetime(2024, 6, 1, tzinfo=UTC)
        repository.update_last_watched(conn, "m1", [{"viewed_at": old}])
        repository.update_last_watched(conn, "m1", [{"viewed_at": recent}])
        row = conn.execute("SELECT last_watched_at FROM media_items WHERE id='m1'").fetchone()
        assert "2024-06-01" in row["last_watched_at"]


# ---------------------------------------------------------------------------
# schedule_deletion — race handling (Domain 05)
# ---------------------------------------------------------------------------


class TestScheduleDeletionRace:
    def test_returns_scheduled_on_success(self, conn):
        _insert_item(conn)
        result = schedule_deletion(
            conn,
            media_id="m1",
            is_reentry=False,
            grace_days=14,
            secret_key="0123456789abcdef" * 4,
        )
        assert result == "scheduled"
        assert (
            conn.execute(
                "SELECT id FROM scheduled_actions "
                "WHERE media_item_id='m1' AND action='scheduled_deletion'"
            ).fetchone()
            is not None
        )

    def test_returns_skipped_on_concurrent_active_deletion(self, conn):
        """A second concurrent run lining up the same item must not raise.

        The migration-25 partial unique index enforces "one active
        pending deletion per item" — a sibling worker that lost the race
        should observe the existing row and skip cleanly, not bubble
        IntegrityError up to the caller.
        """
        _insert_item(conn)
        first = schedule_deletion(
            conn,
            media_id="m1",
            is_reentry=False,
            grace_days=14,
            secret_key="0123456789abcdef" * 4,
        )
        assert first == "scheduled"
        # Second call hits the partial unique index — must report skipped.
        second = schedule_deletion(
            conn,
            media_id="m1",
            is_reentry=False,
            grace_days=14,
            secret_key="0123456789abcdef" * 4,
        )
        assert second == "skipped"
        # Only one active row remains.
        rows = conn.execute(
            "SELECT id FROM scheduled_actions "
            "WHERE media_item_id='m1' AND action='scheduled_deletion' "
            "AND token_used=0 AND (delete_status IS NULL OR delete_status='pending')"
        ).fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# delete_media_items + delete_actions_for_media_items — atomic two-table delete
# ---------------------------------------------------------------------------


class TestDeleteMediaItemsAtomic:
    def test_two_table_delete_under_one_transaction_clears_both(self, conn):
        """The delete phase wraps both repository DELETEs in one
        ``with conn:`` transaction so they commit (or roll back)
        together. After the pair runs, both tables are empty for m1.
        """
        _insert_item(conn, media_id="m1")
        _insert_action(conn, media_id="m1")
        with conn:
            repository.delete_actions_for_media_items(conn, ["m1"])
            repository.delete_media_items(conn, ["m1"])
        # Both tables must be empty for m1.
        assert conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone() is None
        assert (
            conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id='m1'").fetchone()
            is None
        )


# ---------------------------------------------------------------------------
# DeletionRow dataclass — §9.5 typed return shape
# ---------------------------------------------------------------------------


class TestDeletionRowShape:
    """``fetch_pending_deletions`` / ``fetch_stuck_deletions`` return a
    frozen, slotted ``DeletionRow`` dataclass — never a raw ``sqlite3.Row``.
    """

    def test_pending_deletions_return_deletion_rows(self, conn):
        _insert_item(conn, media_id="m1", title="Pending Film", file_path="/m/p.mkv")
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        _insert_action(conn, media_id="m1", action="scheduled_deletion", execute_at=past)
        rows = repository.fetch_pending_deletions(conn, datetime.now(UTC).isoformat())
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, repository.DeletionRow)
        # Attribute access — not subscript.
        assert row.media_item_id == "m1"
        assert row.title == "Pending Film"
        assert row.file_path == "/m/p.mkv"
        assert row.action == "scheduled_deletion"

    def test_stuck_deletions_return_deletion_rows(self, conn):
        _insert_item(conn, media_id="m2", title="Stuck Film")
        _insert_action(conn, media_id="m2", action="scheduled_deletion", delete_status="deleting")
        rows = repository.fetch_stuck_deletions(conn)
        assert len(rows) == 1
        assert isinstance(rows[0], repository.DeletionRow)
        assert rows[0].media_item_id == "m2"
        assert rows[0].title == "Stuck Film"

    def test_deletion_row_is_frozen_and_slotted(self):
        row = repository.DeletionRow(
            id=1,
            media_item_id="m1",
            action="scheduled_deletion",
            file_path="/m/x.mkv",
            file_size_bytes=10,
            title="X",
            plex_rating_key="rk",
            radarr_id=None,
            sonarr_id=None,
            season_number=None,
        )
        with pytest.raises((AttributeError, TypeError)):
            row.title = "mutated"  # type: ignore[misc]
        # slots=True — no __dict__.
        assert not hasattr(row, "__dict__")


# ---------------------------------------------------------------------------
# count_pending_deletions — §9.4 count split out of clear_pending_deletions
# ---------------------------------------------------------------------------


class TestCountPendingDeletions:
    def test_counts_only_unused_scheduled_deletions(self, conn):
        _insert_item(conn, media_id="m1")
        _insert_item(conn, media_id="m2")
        _insert_item(conn, media_id="m3")
        _insert_action(conn, media_id="m1", action="scheduled_deletion", token="t1", token_used=0)
        _insert_action(conn, media_id="m2", action="scheduled_deletion", token="t2", token_used=0)
        # token_used=1 must NOT count.
        _insert_action(conn, media_id="m3", action="scheduled_deletion", token="t3", token_used=1)
        assert repository.count_pending_deletions(conn) == 2

    def test_zero_when_no_pending(self, conn):
        _insert_item(conn)
        assert repository.count_pending_deletions(conn) == 0

    def test_count_matches_clear_return_value(self, conn):
        """``count_pending_deletions`` returns exactly what
        ``clear_pending_deletions`` reports it removed."""
        _insert_item(conn, media_id="m1")
        _insert_item(conn, media_id="m2")
        _insert_action(conn, media_id="m1", action="scheduled_deletion", token="t1")
        _insert_action(conn, media_id="m2", action="scheduled_deletion", token="t2")
        counted = repository.count_pending_deletions(conn)
        cleared = repository.clear_pending_deletions(conn)
        assert counted == cleared == 2


# ---------------------------------------------------------------------------
# Batched guard sets — §13.3 N+1 elimination parity
# ---------------------------------------------------------------------------


class TestBatchedGuardSetParity:
    """The batched ``fetch_protected_media_ids`` /
    ``fetch_already_scheduled_media_ids`` set builders must return EXACTLY
    the ids for which the per-item ``is_protected`` / ``is_already_scheduled``
    predicates return True — the protection decision must be identical
    before and after the N+1 fix.
    """

    def _seed_mixed_population(self, conn):
        """Five items, one per protection state, plus their action rows."""
        now = datetime.now(UTC)
        future = (now + timedelta(days=7)).isoformat()
        past = (now - timedelta(days=1)).isoformat()
        # protected_forever
        _insert_item(conn, media_id="prot")
        _insert_action(conn, media_id="prot", action="protected_forever", token="pf")
        # active snooze
        _insert_item(conn, media_id="snoozed")
        _insert_action(conn, media_id="snoozed", action="snoozed", token="sn", execute_at=future)
        # expired snooze — NOT protected
        _insert_item(conn, media_id="expired")
        _insert_action(conn, media_id="expired", action="snoozed", token="snx", execute_at=past)
        # pending scheduled_deletion — already scheduled, NOT protected
        _insert_item(conn, media_id="sched")
        _insert_action(conn, media_id="sched", action="scheduled_deletion", token="sd")
        # unprotected, no action rows at all
        _insert_item(conn, media_id="plain")
        return ["prot", "snoozed", "expired", "sched", "plain"]

    def test_protected_set_matches_per_item_is_protected(self, conn):
        media_ids = self._seed_mixed_population(conn)
        now_iso_str = datetime.now(UTC).isoformat()
        batched = repository.fetch_protected_media_ids(conn, media_ids, now_iso_str)
        per_item = {mid for mid in media_ids if repository.is_protected(conn, mid)}
        assert batched == per_item
        # Sanity: exactly protected_forever + active snooze.
        assert batched == {"prot", "snoozed"}

    def test_already_scheduled_set_matches_per_item(self, conn):
        media_ids = self._seed_mixed_population(conn)
        batched = repository.fetch_already_scheduled_media_ids(conn, media_ids)
        per_item = {mid for mid in media_ids if repository.is_already_scheduled(conn, mid)}
        assert batched == per_item
        assert batched == {"sched"}

    def test_used_token_scheduled_deletion_not_in_set(self, conn):
        """A consumed ``scheduled_deletion`` token must be excluded — matches
        ``is_already_scheduled``'s ``token_used = 0`` predicate."""
        _insert_item(conn, media_id="used")
        _insert_action(conn, media_id="used", action="scheduled_deletion", token="ut", token_used=1)
        batched = repository.fetch_already_scheduled_media_ids(conn, ["used"])
        assert batched == set()
        assert repository.is_already_scheduled(conn, "used") is False

    def test_empty_media_ids_returns_empty_sets(self, conn):
        assert (
            repository.fetch_protected_media_ids(conn, [], datetime.now(UTC).isoformat()) == set()
        )
        assert repository.fetch_already_scheduled_media_ids(conn, []) == set()

    def test_chunking_above_500_preserves_membership(self, conn):
        """The 500-id chunk loop must not drop or duplicate ids — seed 600
        items, half protected, and assert the batched set is exact."""
        protected_ids = []
        all_ids = []
        for i in range(600):
            mid = f"c{i}"
            _insert_item(conn, media_id=mid)
            all_ids.append(mid)
            if i % 2 == 0:
                _insert_action(conn, media_id=mid, action="protected_forever", token=f"pf-{i}")
                protected_ids.append(mid)
        batched = repository.fetch_protected_media_ids(conn, all_ids, datetime.now(UTC).isoformat())
        assert batched == set(protected_ids)
        assert len(batched) == 300
