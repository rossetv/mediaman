"""Tests for a deletion that takes more than one pass, and for what proves a file was removed."""

from __future__ import annotations

import json

import pytest

from mediaman.db import init_db
from mediaman.scanner.deletions import DeletionExecutor
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
