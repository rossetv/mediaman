"""Tests for the deletion pass's Radarr/Sonarr unmonitor, root state and season boundary."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from mediaman.core.delete_root_state import read_delete_root_state
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


class TestRootState:
    def _drop_mount(self, root):
        """Empty *root* as an unmounted mount point leaves it."""
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            for name in filenames:
                os.remove(os.path.join(dirpath, name))
            for name in dirnames:
                os.rmdir(os.path.join(dirpath, name))

    def test_items_waiting_on_an_empty_root_are_counted_and_cleared_when_it_returns(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "movies"
        root.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        for name in ("a", "b"):
            path = root / name / f"{name}.mkv"
            _item(conn, name, file_path=path, files=[path])

        DeletionExecutor(conn=conn).execute()

        assert read_delete_root_state(conn) == {str(root): 2}
        assert _status(conn, "a") == "pending"

        (root / "keep.txt").write_text("mounted")
        DeletionExecutor(conn=conn).execute()

        assert read_delete_root_state(conn) == {}

    def test_dry_run_does_not_write_the_state(self, conn, tmp_path, monkeypatch):
        root = tmp_path / "movies"
        root.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        path = root / "a" / "a.mkv"
        _item(conn, "a", file_path=path, files=[path])

        DeletionExecutor(conn=conn, dry_run=True, cleanup_snoozes=False).execute()

        assert read_delete_root_state(conn) == {}

    def test_one_audit_row_per_unavailable_root_is_written_once_across_passes(
        self, conn, tmp_path, monkeypatch
    ):
        movies, tv = tmp_path / "movies", tmp_path / "tv"
        movies.mkdir()
        tv.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", f"{movies}:{tv}")
        for name, root in (("a", movies), ("b", movies), ("c", tv)):
            path = root / name / f"{name}.mkv"
            _item(conn, name, file_path=path, files=[path])

        DeletionExecutor(conn=conn).execute()
        DeletionExecutor(conn=conn).execute()

        details = [
            r["detail"]
            for r in conn.execute(
                "SELECT detail FROM audit_log WHERE action = 'delete_root_unavailable' "
                "AND media_item_id NOT IN ('a', 'b', 'c')"
            )
        ]
        assert len(details) == 2
        movies_row = next(d for d in details if f"'{movies}'" in d)
        tv_row = next(d for d in details if f"'{tv}'" in d)
        assert "2 scheduled deletions" in movies_row and "1 scheduled deletion " in tv_row
        assert len(_audit(conn, "a", "delete_root_unavailable")) == 1

    def test_a_tracked_files_folder_decides_which_folder_is_unavailable(
        self, conn, tmp_path, monkeypatch
    ):
        # file_path still exists, but the tracked episodes sit on a dropped mount below the root.
        root = tmp_path / "tv"
        show = root / "Show"
        mount = show / "Season 1"
        mount.mkdir(parents=True)
        (show / "poster.jpg").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        episode = mount / "Show - s01e01.mkv"
        _item(
            conn,
            "s1",
            file_path=show,
            files=[episode],
            media_type="tv_season",
            season_number=1,
        )

        DeletionExecutor(conn=conn).execute()

        assert read_delete_root_state(conn) == {str(mount): 1}
        assert _status(conn, "s1") == "pending"
        assert str(mount) in _audit(conn, "s1", "delete_root_unavailable")[0]

    def test_file_path_decides_when_the_tracked_files_are_not_recorded(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "movies"
        root.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        path = root / "a" / "a.mkv"
        _item(conn, "a", file_path=path, files=[])
        conn.execute("UPDATE media_items SET tracked_files = NULL WHERE id = 'a'")
        conn.commit()

        DeletionExecutor(conn=conn).execute()

        assert read_delete_root_state(conn) == {str(root): 1}


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
