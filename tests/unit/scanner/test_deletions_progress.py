"""Tests for a deletion that takes more than one pass, and for what proves a file was removed."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mediaman.db import init_db
from mediaman.scanner.deletions import DeletionExecutor, recover_stuck_deletions
from mediaman.services.infra.storage import deletion as storage
from tests.helpers.factories import insert_media_item, insert_scheduled_action


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _actions(conn, action):
    return conn.execute(
        "SELECT detail, space_reclaimed_bytes FROM audit_log WHERE media_item_id = 'm1' "
        "AND action = ? ORDER BY id",
        (action,),
    ).fetchall()


def _status(conn):
    row = conn.execute(
        "SELECT delete_status FROM scheduled_actions WHERE media_item_id = 'm1'"
    ).fetchone()
    return row["delete_status"] if row else None


@pytest.fixture
def two_parts(conn, tmp_path, monkeypatch):
    """A due movie whose two parts sit in two folders: cd1 (4 bytes) and cd2 (1 byte)."""
    root = tmp_path / "movies"
    free, locked = root / "Free", root / "Locked"
    free.mkdir(parents=True)
    locked.mkdir()
    cd1, cd2 = free / "Movie - cd1.mkv", locked / "Movie - cd2.mkv"
    cd1.write_bytes(b"x" * 4)
    cd2.write_bytes(b"x")
    monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
    insert_media_item(conn, id="m1", title="Movie", plex_rating_key="m1", file_path=str(cd1))
    conn.execute(
        "UPDATE media_items SET tracked_files = ? WHERE id = 'm1'",
        (json.dumps([{"path": str(cd1), "size": 4}, {"path": str(cd2), "size": 1}]),),
    )
    insert_scheduled_action(
        conn,
        media_item_id="m1",
        action="scheduled_deletion",
        token="tok-m1",
        execute_at="2020-01-01",
        token_used=False,
        delete_status="pending",
    )
    conn.commit()
    return cd1, cd2


def _pass_with_cd2_locked(conn, cd2):
    cd2.parent.chmod(0o555)
    try:
        return DeletionExecutor(conn=conn).execute()
    finally:
        cd2.parent.chmod(0o755)


class TestRemovalOverTwoPasses:
    def test_a_file_removed_by_an_earlier_pass_counts_as_removed_and_the_item_closes(
        self, conn, two_parts
    ):
        cd1, cd2 = two_parts
        _pass_with_cd2_locked(conn, cd2)
        assert not cd1.exists() and _status(conn) == "pending"

        result = DeletionExecutor(conn=conn).execute()

        assert not cd2.exists()
        assert result["deleted"] == 1
        assert _status(conn) is None
        assert len(_actions(conn, "deleted")) == 1

    def test_the_deleted_row_carries_the_bytes_of_every_pass_and_the_incomplete_row_none(
        self, conn, two_parts
    ):
        _, cd2 = two_parts
        first = _pass_with_cd2_locked(conn, cd2)
        second = DeletionExecutor(conn=conn).execute()

        assert (first["reclaimed_bytes"], second["reclaimed_bytes"]) == (4, 1)
        assert [r["space_reclaimed_bytes"] for r in _actions(conn, "deleted")] == [5]
        assert [r["space_reclaimed_bytes"] for r in _actions(conn, "delete_incomplete")] == [None]

    def test_a_crash_inside_the_removal_keeps_the_record_of_the_files_already_gone(
        self, conn, two_parts, monkeypatch
    ):
        # Resolving cd2's folder fails after cd1 is unlinked.
        cd1, cd2 = two_parts
        real_unlink, real_resolve = storage._unlink_regular_file, Path.resolve
        armed = []

        def unlink(target, roots):
            armed.append(target)
            return real_unlink(target, roots)

        def resolve(self, strict=False):
            if armed and self == cd2.parent:
                raise OSError("stale handle")
            return real_resolve(self, strict=strict)

        monkeypatch.setattr(storage, "_unlink_regular_file", unlink)
        monkeypatch.setattr(Path, "resolve", resolve)
        DeletionExecutor(conn=conn).execute()
        monkeypatch.undo()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(cd1.parent.parent))

        assert not cd1.exists() and cd2.exists()
        assert _actions(conn, "delete_failed_permanent") == []

        DeletionExecutor(conn=conn).execute()

        assert _status(conn) is None
        assert [r["space_reclaimed_bytes"] for r in _actions(conn, "deleted")] == [5]


class TestFolderCleanupFailure:
    def test_a_failure_removing_bare_folders_still_books_the_removed_files(
        self, conn, two_parts, monkeypatch
    ):
        def broken(*args, **kwargs):
            raise OSError("folder busy")

        monkeypatch.setattr("mediaman.scanner.deletions.remove_empty_folders", broken)

        result = DeletionExecutor(conn=conn).execute()

        assert result == {"deleted": 1, "reclaimed_bytes": 5}
        assert _status(conn) is None
        assert _actions(conn, "delete_failed_permanent") == []
        assert "folder busy" in _actions(conn, "delete_folders_failed")[0]["detail"]


class TestRecoveryProof:
    def _stuck(self, conn, two_parts):
        conn.execute("UPDATE scheduled_actions SET delete_status = 'deleting'")
        conn.commit()

    def test_files_gone_without_a_record_of_removing_them_are_not_booked_deleted(
        self, conn, two_parts, tmp_path
    ):
        # Both parts moved away while the row was stuck; the folders still hold other files.
        for part in two_parts:
            os.rename(part, tmp_path / part.name)
            (part.parent / "other.mkv").write_bytes(b"x")
        self._stuck(conn, two_parts)

        booked = recover_stuck_deletions(conn)

        assert booked == []
        assert _status(conn) == "pending"
        assert _actions(conn, "deleted") == []
        assert len(_actions(conn, "delete_awaiting_scan")) == 1

    def test_files_this_deletion_recorded_removing_are_booked_with_the_recorded_bytes(
        self, conn, two_parts
    ):
        cd1, cd2 = two_parts
        _pass_with_cd2_locked(conn, cd2)
        cd2.unlink()
        (cd2.parent / "poster.jpg").write_bytes(b"p")
        conn.execute(
            "UPDATE scheduled_actions SET removed_files = json_set(removed_files, '$.paths',"
            " json_array(?, ?)), delete_status = 'deleting'",
            (str(cd1), str(cd2)),
        )
        conn.commit()

        recover_stuck_deletions(conn)

        assert _status(conn) is None
        assert [r["space_reclaimed_bytes"] for r in _actions(conn, "deleted")] == [4]
