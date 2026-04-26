"""Tests for mediaman.scanner.deletions.

Covers: DeletionExecutor.execute (dry_run, no allowed roots, actual delete
path, stuck-state recovery) and _recover_stuck_deletions.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.scanner.deletions import DeletionExecutor, _recover_stuck_deletions


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _insert_media(conn, *, media_id="m1", title="Test Film", file_path="/tmp/test.mkv"):
    conn.execute(
        "INSERT INTO media_items "
        "(id, title, media_type, plex_library_id, plex_rating_key, added_at, file_path, file_size_bytes) "
        "VALUES (?, ?, 'movie', 1, ?, '2020-01-01', ?, 1000000)",
        (media_id, title, media_id, file_path),
    )


def _insert_pending_deletion(conn, *, media_id="m1", execute_at=None):
    """Insert a scheduled_deletion row with execute_at in the past."""
    if execute_at is None:
        execute_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn.execute(
        "INSERT INTO scheduled_actions "
        "(media_item_id, action, scheduled_at, execute_at, token, token_used, delete_status) "
        "VALUES (?, 'scheduled_deletion', '2020-01-01', ?, 'tok-' || ?, 0, 'pending')",
        (media_id, execute_at, media_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_delete_file(self, conn, tmp_path):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        executor = DeletionExecutor(conn=conn, dry_run=True)
        result = executor.execute()

        assert result["deleted"] == 0
        assert real_file.exists()

    def test_dry_run_writes_audit_entry(self, conn, tmp_path):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        DeletionExecutor(conn=conn, dry_run=True).execute()

        row = conn.execute("SELECT action FROM audit_log WHERE media_item_id='m1'").fetchone()
        assert row is not None
        assert row["action"] == "dry_run_skip"


# ---------------------------------------------------------------------------
# no allowed roots
# ---------------------------------------------------------------------------


class TestNoAllowedRoots:
    def test_skips_deletion_when_roots_not_configured(self, conn, tmp_path, monkeypatch):
        # Ensure no roots in DB or env.
        conn.execute("DELETE FROM settings WHERE key='delete_allowed_roots'")
        conn.commit()
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)

        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        assert result["deleted"] == 0
        assert real_file.exists()


# ---------------------------------------------------------------------------
# successful deletion
# ---------------------------------------------------------------------------


class TestSuccessfulDeletion:
    def test_deletes_file_and_returns_count(self, conn, tmp_path, monkeypatch):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        assert result["deleted"] == 1
        assert not real_file.exists()

    def test_reclaimed_bytes_summed(self, conn, tmp_path, monkeypatch):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"x" * 1_000)

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        # We stored file_size_bytes=1000000 in the DB row.
        assert result["reclaimed_bytes"] == 1_000_000

    def test_audit_log_entry_written(self, conn, tmp_path, monkeypatch):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        DeletionExecutor(conn=conn, dry_run=False).execute()

        row = conn.execute("SELECT action FROM audit_log WHERE media_item_id='m1'").fetchone()
        assert row is not None
        assert row["action"] == "deleted"

    def test_calls_radarr_unmonitor(self, conn, tmp_path, monkeypatch):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))

        # Give the media item a radarr_id.
        conn.execute(
            "INSERT INTO media_items "
            "(id, title, media_type, plex_library_id, plex_rating_key, added_at, "
            "file_path, file_size_bytes, radarr_id) "
            "VALUES ('r1', 'Radarr Film', 'movie', 1, 'r1', '2020-01-01', ?, 100, 42)",
            (str(real_file),),
        )
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, execute_at, token, token_used, delete_status) "
            "VALUES ('r1', 'scheduled_deletion', '2020-01-01', '2020-01-01', 'tok-r1', 0, 'pending')"
        )
        conn.commit()

        fake_radarr = MagicMock()
        DeletionExecutor(conn=conn, dry_run=False, radarr_client=fake_radarr).execute()

        fake_radarr.unmonitor_movie.assert_called_once_with(42)

    def test_future_deletions_are_not_executed(self, conn, tmp_path, monkeypatch):
        real_file = tmp_path / "future.mkv"
        real_file.write_bytes(b"data")

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))

        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn, execute_at=future)

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        assert result["deleted"] == 0
        assert real_file.exists()


# ---------------------------------------------------------------------------
# _recover_stuck_deletions
# ---------------------------------------------------------------------------


class TestRecoverStuckDeletions:
    def test_resets_to_pending_when_file_present(self, conn, tmp_path):
        real_file = tmp_path / "stuck.mkv"
        real_file.write_bytes(b"data")

        _insert_media(conn, file_path=str(real_file))
        conn.execute(
            "UPDATE scheduled_actions SET delete_status='deleting' WHERE media_item_id='m1'"
        )
        conn.commit()

        # Need a row — insert one manually in deleting state.
        _insert_media(conn, media_id="m2", file_path=str(real_file))
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, token, token_used, delete_status) "
            "VALUES ('m2', 'scheduled_deletion', '2020-01-01', 'tok-m2', 0, 'deleting')"
        )
        conn.commit()

        _recover_stuck_deletions(conn)

        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='m2'"
        ).fetchone()
        assert row["delete_status"] == "pending"

    def test_completes_cleanup_when_file_gone(self, conn, tmp_path):
        # File does NOT exist — should log as already-deleted and remove the row.
        missing_path = str(tmp_path / "already_gone.mkv")

        _insert_media(conn, media_id="m3", file_path=missing_path)
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, token, token_used, delete_status) "
            "VALUES ('m3', 'scheduled_deletion', '2020-01-01', 'tok-m3', 0, 'deleting')"
        )
        conn.commit()

        _recover_stuck_deletions(conn)

        row = conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id='m3'").fetchone()
        assert row is None  # Row removed — cleanup complete.

    def test_no_stuck_rows_is_noop(self, conn):
        # Must not raise when there are no deleting rows.
        _recover_stuck_deletions(conn)
