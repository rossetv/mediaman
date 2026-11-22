"""Tests for the deletion pass's Radarr/Sonarr unmonitor, root state and season boundary."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.bootstrap.scan_jobs import _recover_stuck_deletions_at_boot
from mediaman.core.delete_root_state import read_delete_root_state
from mediaman.db import init_db
from mediaman.scanner.deletions import DeletionExecutor, recover_stuck_deletions
from mediaman.services.infra.storage import deletion as storage
from tests.helpers.factories import insert_media_item, insert_scheduled_action


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _track(conn, media_id, files):
    """Record *files* as the item's tracked files, sized as they are on disk."""
    tracked = [
        {"path": str(p), "size": os.path.getsize(p) if os.path.lexists(p) else 0} for p in files
    ]
    conn.execute(
        "UPDATE media_items SET tracked_files = ? WHERE id = ?", (json.dumps(tracked), media_id)
    )
    conn.commit()


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
    _track(conn, media_id, files)
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
def movie(conn, tmp_path, monkeypatch):
    """A due movie at <root>/Film (2020)/film.mkv with TMDB id 603 and Radarr id 42."""
    root = tmp_path / "movies"
    folder = root / "Film (2020)"
    folder.mkdir(parents=True)
    (root / "Other").mkdir()
    (root / "Other" / "other.mkv").write_bytes(b"x")
    video = folder / "film.mkv"
    video.write_bytes(b"data")
    monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
    _item(conn, "m1", file_path=video, files=[video], radarr_id=42, tmdb_id=603)
    return video


def _radarr(entry):
    radarr = MagicMock()
    radarr.get_movie_by_id.return_value = entry
    radarr.get_movie_by_tmdb.return_value = entry
    return radarr


_MOVIE = {"id": 42, "tmdbId": 603, "path": "/data/movies/Film (2020)"}


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


class TestVerifiedUnmonitor:
    def test_a_deleted_movie_matching_radarr_is_unmonitored_without_deleting_files(
        self, conn, movie
    ):
        radarr = _radarr(_MOVIE)

        result = DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert result["deleted"] == 1
        radarr.unmonitor_movie.assert_called_once_with(42)
        radarr.delete_movie.assert_not_called()
        assert _audit(conn, "m1", "unmonitor_skipped") == []

    def test_a_movie_without_a_stored_radarr_id_is_found_by_tmdb_id(self, conn, movie):
        conn.execute("UPDATE media_items SET radarr_id = NULL WHERE id = 'm1'")
        conn.commit()
        radarr = _radarr(_MOVIE)

        DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        radarr.get_movie_by_tmdb.assert_called_once_with(603)
        radarr.unmonitor_movie.assert_called_once_with(42)

    def test_a_deleted_season_matching_sonarr_is_unmonitored_without_deleting_files(
        self, conn, season
    ):
        sonarr = _sonarr(_SERIES)

        result = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert result["deleted"] == 1
        sonarr.unmonitor_season.assert_called_once_with(7, 1)
        sonarr.delete_episode_files.assert_not_called()
        sonarr.delete_series.assert_not_called()

    @pytest.mark.parametrize(
        ("entry", "reason"),
        [
            ({**_MOVIE, "tmdbId": 604}, "TMDB id"),
            ({**_MOVIE, "path": "/data/movies-4k/Film (2020) 4K"}, "folder"),
            (None, "no entry"),
        ],
        ids=["other-id", "other-folder", "missing"],
    )
    def test_a_movie_radarr_does_not_match_is_booked_deleted_and_not_unmonitored(
        self, conn, movie, entry, reason
    ):
        conn.execute("UPDATE media_items SET radarr_id = NULL WHERE id = 'm1'")
        conn.commit()
        radarr = _radarr(entry)

        result = DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert result["deleted"] == 1
        assert not movie.exists()
        radarr.unmonitor_movie.assert_not_called()
        skipped = _audit(conn, "m1", "unmonitor_skipped")
        assert len(skipped) == 1 and reason in skipped[0]

    @pytest.mark.parametrize(
        ("series", "reason"),
        [
            ({**_SERIES, "tvdbId": 1}, "TVDB id"),
            ({**_SERIES, "seasons": [{"seasonNumber": 2}]}, "season"),
            ({**_SERIES, "path": "/data/tv/Another Show"}, "folder"),
        ],
        ids=["other-id", "no-season", "other-folder"],
    )
    def test_a_season_sonarr_does_not_match_is_not_unmonitored(self, conn, season, series, reason):
        sonarr = _sonarr(series)

        result = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert result["deleted"] == 1
        sonarr.unmonitor_season.assert_not_called()
        skipped = _audit(conn, "s1", "unmonitor_skipped")
        assert len(skipped) == 1 and reason in skipped[0]

    def test_a_movie_whose_radarr_file_is_the_deleted_one_is_unmonitored(self, conn, movie):
        radarr = _radarr(
            {**_MOVIE, "hasFile": True, "movieFile": {"relativePath": "film.mkv", "size": 4}}
        )

        DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        radarr.unmonitor_movie.assert_called_once_with(42)

    @pytest.mark.parametrize(
        "movie_file",
        [
            {"relativePath": "film.mkv", "size": 9_000},
            {"relativePath": "Film (2020) Bluray-1080p.mkv", "size": 4},
            None,
        ],
        ids=["other-size", "other-name", "file-not-described"],
    )
    def test_a_movie_whose_radarr_file_was_not_deleted_is_not_unmonitored(
        self, conn, movie, movie_file
    ):
        # Radarr's entry for the same film and folder name holds a copy mediaman kept.
        entry = {**_MOVIE, "hasFile": True}
        if movie_file is not None:
            entry["movieFile"] = movie_file
        radarr = _radarr(entry)

        result = DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert result["deleted"] == 1
        radarr.unmonitor_movie.assert_not_called()
        assert len(_audit(conn, "m1", "unmonitor_skipped")) == 1

    def test_a_season_whose_sonarr_files_are_the_deleted_ones_is_unmonitored(self, conn, season):
        sonarr = _sonarr(_SERIES)
        sonarr.get_episode_files.return_value = [
            {"seasonNumber": 1, "relativePath": "Season 1/Show - s01e01.mkv", "size": 1},
            {"seasonNumber": 2, "relativePath": "Season 2/Show - s02e01.mkv", "size": 5},
        ]

        DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        sonarr.unmonitor_season.assert_called_once_with(7, 1)

    def test_a_season_with_a_sonarr_file_mediaman_did_not_delete_is_not_unmonitored(
        self, conn, season
    ):
        sonarr = _sonarr(_SERIES)
        sonarr.get_episode_files.return_value = [
            {"seasonNumber": 1, "relativePath": "Season 1/Show - s01e01.mkv", "size": 1},
            {"seasonNumber": 1, "relativePath": "Season 1/Show - s01e02.mkv", "size": 1},
        ]

        result = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert result["deleted"] == 1
        sonarr.unmonitor_season.assert_not_called()
        assert len(_audit(conn, "s1", "unmonitor_skipped")) == 1

    @staticmethod
    def _two_episodes(conn, season):
        """Track a second episode beside *season* and return Sonarr listing both."""
        e02 = season.parent / "Show - s01e02.mkv"
        e02.write_bytes(b"yy")
        _track(conn, "s1", [season, e02])
        sonarr = _sonarr(_SERIES)
        sonarr.get_episode_files.return_value = [
            {"seasonNumber": 1, "relativePath": "Season 1/Show - s01e01.mkv", "size": 1},
            {"seasonNumber": 1, "relativePath": "Season 1/Show - s01e02.mkv", "size": 2},
        ]
        return e02, sonarr

    def test_a_file_an_earlier_pass_removed_still_matches_after_a_scan_drops_it(
        self, conn, season, monkeypatch
    ):
        e02, sonarr = self._two_episodes(conn, season)
        unlink = storage._unlink_regular_file

        def e02_fails(path, roots):
            if path.name == e02.name:
                raise PermissionError("busy")
            return unlink(path, roots)

        monkeypatch.setattr(storage, "_unlink_regular_file", e02_fails)
        DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()
        monkeypatch.setattr(storage, "_unlink_regular_file", unlink)
        _track(conn, "s1", [e02])

        result = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert result["deleted"] == 1
        sonarr.unmonitor_season.assert_called_once_with(7, 1)

    def test_a_removed_file_recorded_without_its_size_does_not_match(self, conn, season):
        e02, sonarr = self._two_episodes(conn, season)
        season.unlink()
        conn.execute(
            "UPDATE scheduled_actions SET removed_files = ? WHERE media_item_id = 's1'",
            (json.dumps({"paths": [str(season)], "bytes": 1}),),
        )
        _track(conn, "s1", [e02])

        result = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert result["deleted"] == 1
        sonarr.unmonitor_season.assert_not_called()

    def test_a_404_on_the_stored_radarr_id_counts_as_no_entry(self, conn, movie):
        radarr = _radarr(_MOVIE)
        error = requests.HTTPError("not found")
        error.response = MagicMock(status_code=404)
        radarr.get_movie_by_id.side_effect = error

        DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        radarr.unmonitor_movie.assert_not_called()
        assert "no entry" in _audit(conn, "m1", "unmonitor_skipped")[0]

    def test_no_radarr_configured_skips_the_unmonitor_without_an_audit_row(self, conn, movie):
        result = DeletionExecutor(conn=conn).execute()

        assert result["deleted"] == 1
        assert _status(conn, "m1") is None
        assert _audit(conn, "m1", "unmonitor_skipped") == []

    @pytest.mark.parametrize("failing", ["get_movie_by_id", "unmonitor_movie"])
    def test_a_radarr_error_is_audited_and_the_deletion_stands(self, conn, movie, failing):
        radarr = _radarr(_MOVIE)
        getattr(radarr, failing).side_effect = requests.ConnectionError("refused")

        result = DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert result["deleted"] == 1
        assert _audit(conn, "m1", "deleted")
        assert len(_audit(conn, "m1", "unmonitor_skipped")) == 1

    def test_a_deletion_completed_by_crash_recovery_is_unmonitored(self, conn, movie):
        movie.unlink()
        (movie.parent / "poster.jpg").write_bytes(b"x")
        conn.execute(
            "UPDATE scheduled_actions SET delete_status = 'deleting', removed_files = ?",
            (json.dumps({"paths": [str(movie)], "bytes": 4}),),
        )
        conn.commit()
        radarr = _radarr(_MOVIE)

        DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert _audit(conn, "m1", "deleted")
        radarr.unmonitor_movie.assert_called_once_with(42)

    def test_a_deletion_left_stuck_at_boot_is_booked_and_unmonitored_by_the_next_pass(
        self, conn, movie
    ):
        movie.unlink()
        (movie.parent / "poster.jpg").write_bytes(b"x")
        conn.execute(
            "UPDATE scheduled_actions SET delete_status = 'deleting', removed_files = ?",
            (json.dumps({"paths": [str(movie)], "bytes": 4}),),
        )
        conn.commit()

        _recover_stuck_deletions_at_boot(conn)

        assert _status(conn, "m1") == "pending"
        assert _audit(conn, "m1", "deleted") == []

        radarr = _radarr(_MOVIE)
        DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert _status(conn, "m1") is None
        assert (
            conn.execute(
                "SELECT space_reclaimed_bytes FROM audit_log WHERE action = 'deleted'"
            ).fetchone()[0]
            == 4
        )
        radarr.unmonitor_movie.assert_called_once_with(42)

    def test_no_write_transaction_is_open_while_radarr_is_called(self, conn, movie):
        second = movie.parent.parent / "Second (2021)" / "second.mkv"
        second.parent.mkdir()
        second.write_bytes(b"data")
        _item(conn, "m2", file_path=second, files=[second], radarr_id=43, tmdb_id=604)
        removed = {"m1": movie, "m2": second}
        for media_id, video in removed.items():
            video.unlink()
            (video.parent / "poster.jpg").write_bytes(b"x")
            conn.execute(
                "UPDATE scheduled_actions SET delete_status = 'deleting', removed_files = ? "
                "WHERE media_item_id = ?",
                (json.dumps({"paths": [str(video)], "bytes": 4}), media_id),
            )
        conn.commit()
        seen = []

        def lookup(_id):
            seen.append(conn.in_transaction)
            # A different movie, so each row writes an unmonitor_skipped audit.
            return {"id": 99, "tmdbId": 1}

        radarr = MagicMock()
        radarr.get_movie_by_id.side_effect = lookup

        DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert seen == [False, False]
        assert len(_audit(conn, "m1", "unmonitor_skipped")) == 1
        assert len(_audit(conn, "m2", "unmonitor_skipped")) == 1

    def test_dry_run_calls_neither_radarr_nor_sonarr(self, conn, movie):
        radarr, sonarr = _radarr(_MOVIE), _sonarr(_SERIES)

        DeletionExecutor(
            conn=conn,
            dry_run=True,
            cleanup_snoozes=False,
            radarr_client=radarr,
            sonarr_client=sonarr,
        ).execute()

        assert radarr.method_calls == [] and sonarr.method_calls == []
        assert movie.exists()


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

    def test_a_dry_run_refreshes_the_state_and_touches_nothing_else(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "movies"
        root.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        path = root / "a" / "a.mkv"
        _item(conn, "a", file_path=path, files=[path])
        dry_run = DeletionExecutor(conn=conn, dry_run=True, cleanup_snoozes=False)

        dry_run.execute()

        assert read_delete_root_state(conn) == {str(root): 1}
        assert _status(conn, "a") == "pending"
        assert _audit(conn, "a", "delete_root_unavailable") == []

        (root / "keep.txt").write_text("mounted")
        dry_run.execute()

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

    def test_a_root_that_returns_and_drops_again_is_audited_each_time(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "movies"
        root.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        path = root / "a" / "a.mkv"
        _item(conn, "a", file_path=path, files=[path])
        mounted = root / "keep.txt"

        DeletionExecutor(conn=conn).execute()
        mounted.write_text("mounted")
        DeletionExecutor(conn=conn).execute()
        DeletionExecutor(conn=conn).execute()
        mounted.unlink()
        DeletionExecutor(conn=conn).execute()

        rows = conn.execute(
            "SELECT action, detail FROM audit_log WHERE media_item_id = ? ORDER BY id",
            (f"_delete_root:{root}",),
        ).fetchall()
        assert [r["action"] for r in rows] == [
            "delete_root_unavailable",
            "delete_root_available",
            "delete_root_unavailable",
        ]
        assert all(str(root) in r["detail"] for r in rows)

    def test_a_root_left_empty_after_its_waiting_row_goes_is_not_said_to_have_content(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "movies"
        root.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        path = root / "a" / "a.mkv"
        _item(conn, "a", file_path=path, files=[path])
        DeletionExecutor(conn=conn).execute()
        conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = 'a'")
        conn.commit()

        DeletionExecutor(conn=conn).execute()

        [detail] = _audit(conn, f"_delete_root:{root}", "delete_root_available")
        assert str(root) in detail
        assert "content" not in detail

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


class TestRecoveryAfterOwnWalk:
    def test_a_folder_mediaman_emptied_itself_is_not_taken_for_a_dropped_mount(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "media"
        library = root / "Films"
        library.mkdir(parents=True)
        (root / "Other").mkdir()
        (root / "Other" / "o.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        video = library / "Film (2020)" / "film.mkv"
        _item(conn, "m1", file_path=video, files=[video])
        # The removal and its folder walk ran, leaving the library folder empty.
        conn.execute(
            "UPDATE scheduled_actions SET delete_status = 'deleting', removed_files = ?",
            (json.dumps({"paths": [str(video)], "bytes": 4}),),
        )
        conn.commit()

        booked = recover_stuck_deletions(conn)

        assert [row.media_item_id for row in booked] == ["m1"]
        assert _audit(conn, "m1", "delete_root_unavailable") == []


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

    def test_a_season_folder_inside_a_folder_named_like_a_season_never_climbs_above_it(
        self, conn, tmp_path, monkeypatch
    ):
        # "Series 2" may be the show, or a season folder holding a show stored without its own.
        root = tmp_path / "media"
        above = root / "Shows" / "Series 2"
        folder = above / "Season 1"
        folder.mkdir(parents=True)
        (root / "movies").mkdir()
        (root / "movies" / "film.mkv").write_bytes(b"x")
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
        )

        result = DeletionExecutor(conn=conn).execute()

        assert result["deleted"] == 1
        assert not folder.exists()
        assert above.is_dir()

    def test_files_sharing_a_folder_walk_only_as_far_as_the_lower_boundary_allows(
        self, conn, tmp_path, monkeypatch
    ):
        # One folder holds two seasons' episodes: it is season 1's folder, not season 2's.
        root = tmp_path / "tv"
        show = root / "Show"
        folder = show / "Season 1"
        folder.mkdir(parents=True)
        (root / "Other").mkdir()
        (root / "Other" / "o.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        for media_id, number in (("s2", 2), ("s1", 1)):
            episode = folder / f"Show - s0{number}e01.mkv"
            episode.write_bytes(b"x")
            _item(
                conn,
                media_id,
                file_path=folder,
                files=[episode],
                media_type="tv_season",
                season_number=number,
            )

        result = DeletionExecutor(conn=conn).execute()

        assert result["deleted"] == 2
        assert not folder.exists()
        assert show.is_dir()
