"""Tests for the deletion rows and tracked-path reads in scanner.repository.scheduled_actions."""

from __future__ import annotations

import json

import pytest

from mediaman.db import init_db
from mediaman.scanner.repository.scheduled_actions import (
    claim_due_deletions_under,
    fetch_pending_deletions,
    fetch_stuck_deletions,
    fetch_tracked_paths_in_folders,
)
from tests.helpers.factories import insert_media_item, insert_scheduled_action

NOW = "2026-01-01T12:00:00+00:00"


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _item(conn, media_id, tracked, *, file_path="/tv/Show", **ids):
    insert_media_item(conn, id=media_id, plex_rating_key=media_id, file_path=file_path)
    raw = tracked if tracked is None or isinstance(tracked, str) else json.dumps(tracked)
    conn.execute(
        "UPDATE media_items SET tracked_files = ?, tmdb_id = ?, tvdb_id = ? WHERE id = ?",
        (raw, ids.get("tmdb_id"), ids.get("tvdb_id"), media_id),
    )
    conn.commit()


def _due(conn, media_id, status="pending"):
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action="scheduled_deletion",
        token=f"tok-{media_id}",
        execute_at="2025-01-01T00:00:00+00:00",
        token_used=False,
        delete_status=status,
    )


class TestDeletionRowsCarryTrackedFilesAndIds:
    FILES = [{"path": "/tv/Show/e1.mkv", "size": 3}]

    def test_every_deletion_read_returns_tracked_files_and_ids(self, conn):
        _item(conn, "pending", self.FILES, tmdb_id=1, tvdb_id=2)
        _due(conn, "pending")
        _item(conn, "stuck", self.FILES, tmdb_id=1, tvdb_id=2)
        _due(conn, "stuck", status="deleting")
        _item(conn, "under", self.FILES, file_path="/tv/Show/S1", tmdb_id=1, tvdb_id=2)
        _due(conn, "under")

        rows = [
            *fetch_pending_deletions(conn, NOW),
            *fetch_stuck_deletions(conn),
            *claim_due_deletions_under(conn, "pending", "/tv/Show", NOW),
        ]

        assert sorted(r.media_item_id for r in rows) == ["pending", "stuck", "under", "under"]
        for row in rows:
            assert (row.tracked_files, row.tmdb_id, row.tvdb_id) == (self.FILES, 1, 2)
            assert row.media_type == "movie"

    @pytest.mark.parametrize("raw", [None, "not json", '{"path": "/x"}'])
    def test_unrecorded_or_malformed_tracked_files_read_as_none(self, conn, raw):
        _item(conn, "m", raw)
        _due(conn, "m")

        (row,) = fetch_pending_deletions(conn, NOW)

        assert row.tracked_files is None


class TestFetchTrackedPathsInFolders:
    def test_returns_other_items_paths_directly_in_the_folders(self, conn):
        _item(conn, "self", [{"path": "/tv/Show/S1/e1.mkv", "size": 1}])
        _item(conn, "same", [{"path": "/tv/Show/S1/e2.mkv", "size": 1}])
        _item(conn, "deeper", [{"path": "/tv/Show/S1/extra/e3.mkv", "size": 1}])
        _item(conn, "prefix", [{"path": "/tv/Show/S10/e1.mkv", "size": 1}])
        _item(conn, "bad", "not json")
        _item(conn, "odd", json.dumps(["/tv/Show/S1/bare.mkv", {"size": 1}]))
        _item(conn, "none", None)

        paths = fetch_tracked_paths_in_folders(conn, {"/tv/Show/S1"}, ["self"])

        assert paths == {"/tv/Show/S1/e2.mkv"}
