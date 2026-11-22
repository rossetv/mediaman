"""Tests for the deletion pass's Radarr/Sonarr unmonitor, root state and season boundary."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.scanner.deletions import DeletionExecutor
from tests.helpers.factories import insert_media_item, insert_scheduled_action


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _item(conn, media_id, *, file_path, files, **fields):
    """Insert a due item tracking *files*; ``tmdb_id``/``tvdb_id`` go in after the factory."""
    ids = {k: fields.pop(k) for k in ("tmdb_id", "tvdb_id") if k in fields}
    insert_media_item(
        conn,
        id=media_id,
        title=media_id,
        plex_rating_key=media_id,
        file_path=str(file_path),
        **fields,
    )
    tracked = [
        {"path": str(p), "size": os.path.getsize(p) if os.path.lexists(p) else 0} for p in files
    ]
    conn.execute(
        "UPDATE media_items SET tracked_files = ? WHERE id = ?", (json.dumps(tracked), media_id)
    )
    for column, value in ids.items():
        conn.execute(f"UPDATE media_items SET {column} = ? WHERE id = ?", (value, media_id))
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action="scheduled_deletion",
        token=f"tok-{media_id}",
        execute_at="2020-01-01",
        token_used=False,
        delete_status="pending",
    )
    conn.commit()


def _audit(conn, media_id, action):
    return [
        r["detail"]
        for r in conn.execute(
            "SELECT detail FROM audit_log WHERE media_item_id = ? AND action = ? ORDER BY id",
            (media_id, action),
        )
    ]


def _status(conn, media_id):
    row = conn.execute(
        "SELECT delete_status FROM scheduled_actions WHERE media_item_id = ?", (media_id,)
    ).fetchone()
    return row["delete_status"] if row else None


@pytest.fixture
def season(conn, tmp_path, monkeypatch):
    """Due season 1 at <root>/Show/Season 1 with TVDB id 81189 and Sonarr id 7."""
    root = tmp_path / "tv"
    folder = root / "Show" / "Season 1"
    folder.mkdir(parents=True)
    (root / "Other").mkdir()
    (root / "Other" / "o.mkv").write_bytes(b"x")
    episode = folder / "Show - s01e01.mkv"
    episode.write_bytes(b"x")
    monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
    _item(
        conn,
        "s1",
        file_path=folder,
        files=[episode],
        media_type="tv_season",
        season_number=1,
        sonarr_id=7,
        tvdb_id=81189,
    )
    return episode


_SERIES = {
    "id": 7,
    "tvdbId": 81189,
    "path": "/data/tv/Show",
    "seasons": [{"seasonNumber": 1, "monitored": True}],
}


def _sonarr(series):
    sonarr = MagicMock()
    sonarr.get_series_by_id.return_value = series
    sonarr.get_series_by_tvdb.return_value = series
    return sonarr


class TestSeasonFolderBoundary:
    def test_a_season_stored_in_its_show_folder_never_removes_the_folder_above(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "media"
        show = root / "tv" / "Show"
        show.mkdir(parents=True)
        (root / "movies").mkdir()
        (root / "movies" / "film.mkv").write_bytes(b"x")
        episode = show / "Show - s01e01.mkv"
        episode.write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        _item(
            conn,
            "s1",
            file_path=show,
            files=[episode],
            media_type="tv_season",
            season_number=1,
        )

        result = DeletionExecutor(conn=conn).execute()

        assert result["deleted"] == 1
        assert not show.exists()
        assert (root / "tv").is_dir()

    def test_a_bare_season_folder_takes_its_bare_show_folder_with_it(self, conn, tmp_path, season):
        DeletionExecutor(conn=conn, sonarr_client=_sonarr(_SERIES)).execute()

        assert not season.parent.parent.exists()
        assert (tmp_path / "tv").is_dir()
