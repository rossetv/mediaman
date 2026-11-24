"""Tests that what a deletion removed is always booked, whatever happens to its row first."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.scanner import deletions
from mediaman.scanner.deletions import DeletionExecutor, recover_stuck_deletions
from mediaman.scanner.engine import ScanEngine
from mediaman.scanner.repository import clear_pending_deletions
from mediaman.services.infra.storage import deletion as storage
from tests.helpers.factories import insert_kept_show, insert_media_item, insert_scheduled_action

_ADDED = datetime(2020, 1, 1, tzinfo=UTC)


class _Died(BaseException):
    pass


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "movies"
    root.mkdir()
    (root / "other.mkv").write_bytes(b"x")
    monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
    monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)
    monkeypatch.setattr("mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None)
    return root


def _due(conn, media_id, path, *files):
    """Insert a due deletion of a movie at *path* whose tracked *files* hold 4 bytes each."""
    for file in files:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"data")
    insert_media_item(conn, id=media_id, title="Film", plex_rating_key=media_id, file_path=path)
    conn.execute(
        "UPDATE media_items SET tmdb_id = 10, tracked_files = ? WHERE id = ?",
        (json.dumps([{"path": str(file), "size": 4} for file in files]), media_id),
    )
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


@pytest.fixture
def film(conn, root):
    film = root / "Film (2020)" / "film.mkv"
    _due(conn, "m1", str(film), film)
    return film


@pytest.fixture
def two_parts(conn, root):
    cd1, cd2 = root / "Free" / "film - cd1.mkv", root / "Locked" / "film - cd2.mkv"
    _due(conn, "m1", str(cd1), cd1, cd2)
    return cd1, cd2


def _die_before_booking(conn, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(deletions, "_remove_bare_folders", MagicMock(side_effect=_Died))
        with pytest.raises(_Died):
            DeletionExecutor(conn=conn).execute()
    conn.rollback()


def _remove_all_but(conn, monkeypatch, suffix):
    """Run a pass that cannot remove the files whose name ends in *suffix*."""
    real = storage._unlink_regular_file

    def unlink(path, roots):
        if path.name.endswith(suffix):
            raise PermissionError("read-only")
        return real(path, roots)

    with monkeypatch.context() as m:
        m.setattr(storage, "_unlink_regular_file", unlink)
        DeletionExecutor(conn=conn).execute()


def _scan(conn, root, *, radarr=None, listed=()):
    """Return an engine whose Plex lists five other movies, kept, and *listed*."""
    plex = MagicMock()
    plex.get_watch_history.return_value = []
    plex.get_movie_items.return_value = [
        {
            "plex_rating_key": key,
            "title": key,
            "added_at": _ADDED,
            "file_path": str(root / f"{key}.mkv"),
            "file_size_bytes": 1,
            "tracked_files": [{"path": str(root / f"{key}.mkv"), "size": 1}],
        }
        for key in (*(f"o{i}" for i in range(5)), *listed)
    ]
    for i in range(5):
        if not conn.execute("SELECT 1 FROM media_items WHERE id = ?", (f"o{i}",)).fetchone():
            insert_media_item(conn, id=f"o{i}", plex_rating_key=f"o{i}", file_path="")
            insert_scheduled_action(
                conn,
                media_item_id=f"o{i}",
                action="protected_forever",
                token=f"tok-o{i}",
                execute_at=None,
                token_used=True,
                delete_status="pending",
            )
    conn.commit()
    return ScanEngine(
        conn=conn,
        plex_client=plex,
        library_ids=["1"],
        library_types={"1": "movie"},
        secret_key="0123456789abcdef" * 4,
        radarr_client=radarr,
    )


def _booked(conn, media_id="m1"):
    """Return the bytes of each deletion booked with its identity row."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT space_reclaimed_bytes FROM audit_log a JOIN deleted_media d "
            "ON d.audit_id = a.id WHERE a.media_item_id = ? AND a.action = 'deleted'",
            (media_id,),
        )
    ]


def _action(conn, media_id="m1"):
    row = conn.execute(
        "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id = ?", (media_id,)
    ).fetchone()
    return tuple(row) if row else None


class TestPlexDropsAnUnbookedItem:
    @pytest.mark.parametrize("boot", [True, False])
    def test_a_removal_the_process_died_in_is_booked_and_unmonitored(
        self, conn, root, film, monkeypatch, boot
    ):
        _die_before_booking(conn, monkeypatch)
        if boot:
            recover_stuck_deletions(conn, book=False)
        radarr = MagicMock()
        radarr.get_movies.return_value = []
        radarr.get_movie_by_tmdb.return_value = {
            "id": 7,
            "tmdbId": 10,
            "path": str(film.parent),
            "hasFile": False,
        }
        _scan(conn, root).sync_library()

        _scan(conn, root, radarr=radarr).run_scan()

        assert _booked(conn) == [4]
        radarr.unmonitor_movie.assert_called_once_with(7)

    def test_a_removal_whose_sidecar_stayed_is_booked(self, conn, root, film, monkeypatch):
        sidecar = film.with_suffix(".srt")
        sidecar.write_bytes(b"s")
        _remove_all_but(conn, monkeypatch, ".srt")

        _scan(conn, root).run_scan()

        assert _booked(conn) == [5]
        assert not sidecar.exists()

    @pytest.mark.parametrize("still_there", [True, False])
    def test_a_part_removal_is_booked_and_the_other_file_left_alone(
        self, conn, root, two_parts, monkeypatch, still_there
    ):
        _remove_all_but(conn, monkeypatch, "cd2.mkv")
        if not still_there:
            two_parts[1].unlink()

        _scan(conn, root).run_scan()

        assert two_parts[1].exists() is still_there
        assert _booked(conn) == [4]
        assert _action(conn) is None

    def test_the_item_goes_once_its_removal_is_booked(self, conn, root, film, monkeypatch):
        _die_before_booking(conn, monkeypatch)
        _scan(conn, root).run_scan()

        _scan(conn, root).sync_library()

        assert conn.execute("SELECT 1 FROM media_items WHERE id = 'm1'").fetchone() is None

    def test_a_removal_running_now_keeps_its_row(self, conn, root, film):
        conn.execute("UPDATE scheduled_actions SET delete_status = 'deleting'")
        conn.commit()

        _scan(conn, root).sync_library()

        assert _action(conn) == ("scheduled_deletion", "deleting")

    def test_a_deletion_that_removed_nothing_goes_with_its_item(self, conn, root, film):
        _scan(conn, root).sync_library()

        assert _action(conn) is None
        assert film.exists()


class TestRecordWriteFails:
    def test_a_record_write_that_fails_twice_is_tried_again(self, conn, film, monkeypatch):
        real, failures = deletions.record_removed_files, [1, 2]

        def flaky(*args):
            if failures:
                failures.pop()
                raise sqlite3.OperationalError("database is locked")
            real(*args)

        monkeypatch.setattr(deletions, "record_removed_files", flaky)
        DeletionExecutor(conn=conn).execute()

        assert _booked(conn) == [4]


class TestNothingLeftToRemove:
    def test_a_removed_item_is_booked_though_another_item_holds_its_folder(
        self, conn, root, film, monkeypatch
    ):
        folder = film.parent
        conn.execute("UPDATE media_items SET file_path = ? WHERE id = 'm1'", (str(folder),))
        _die_before_booking(conn, monkeypatch)
        recover_stuck_deletions(conn, book=False)
        insert_media_item(conn, id="m2", plex_rating_key="m2", file_path=str(folder / "new.mkv"))
        conn.commit()

        DeletionExecutor(conn=conn).execute()

        assert _booked(conn) == [4]
        assert _action(conn) is None

    @pytest.mark.parametrize("lost", ["file_path = ''", "tracked_files = NULL"])
    def test_a_removed_item_plex_lists_with_no_files_is_booked(self, conn, film, monkeypatch, lost):
        _die_before_booking(conn, monkeypatch)
        recover_stuck_deletions(conn, book=False)
        conn.execute(f"UPDATE media_items SET {lost}")
        conn.commit()

        DeletionExecutor(conn=conn).execute()

        assert _booked(conn) == [4]
        assert _action(conn) is None


class TestKeptBeforeBooked:
    def test_a_show_kept_after_the_files_went_still_books_their_removal(
        self, conn, film, monkeypatch
    ):
        _die_before_booking(conn, monkeypatch)
        recover_stuck_deletions(conn, book=False)
        conn.execute("UPDATE media_items SET show_rating_key = 'show' WHERE id = 'm1'")
        insert_kept_show(conn, show_rating_key="show", action="protected_forever")
        conn.commit()

        DeletionExecutor(conn=conn).execute()

        assert _booked(conn) == [4]
        assert _action(conn) is None


class TestClear:
    @pytest.mark.parametrize("status", ["pending", "deleting"])
    def test_a_fully_removed_item_is_not_cleared_but_left_to_be_booked(
        self, conn, film, monkeypatch, status
    ):
        _die_before_booking(conn, monkeypatch)
        conn.execute("UPDATE scheduled_actions SET delete_status = ?", (status,))
        conn.commit()

        assert clear_pending_deletions(conn) == (0, 1)
        DeletionExecutor(conn=conn).execute()

        assert _booked(conn) == [4]

    @pytest.mark.parametrize(("status", "counts"), [("pending", (1, 0)), ("deleting", (0, 1))])
    def test_a_partly_removed_item_is_cleared_and_books_what_went(
        self, conn, two_parts, monkeypatch, status, counts
    ):
        _remove_all_but(conn, monkeypatch, "cd2.mkv")
        conn.execute("UPDATE scheduled_actions SET delete_status = ?", (status,))
        conn.commit()

        assert clear_pending_deletions(conn) == counts
        DeletionExecutor(conn=conn).execute()

        assert two_parts[1].exists()
        assert _booked(conn) == [4]
        assert _action(conn) is None

    def test_a_clear_during_the_last_removal_credits_each_byte_once(
        self, conn, db_path, two_parts, monkeypatch
    ):
        _remove_all_but(conn, monkeypatch, "cd2.mkv")
        other, real = init_db(str(db_path)), storage._unlink_regular_file

        def unlink(path, roots):
            clear_pending_deletions(other)
            return real(path, roots)

        monkeypatch.setattr(storage, "_unlink_regular_file", unlink)
        DeletionExecutor(conn=conn).execute()

        assert _booked(conn) == [4, 4]

    def test_a_clear_during_a_removal_that_partly_fails_books_what_went(
        self, conn, db_path, two_parts, monkeypatch
    ):
        other, real = init_db(str(db_path)), storage._unlink_regular_file

        def unlink(path, roots):
            clear_pending_deletions(other)
            if path == two_parts[1]:
                raise PermissionError("read-only")
            return real(path, roots)

        monkeypatch.setattr(storage, "_unlink_regular_file", unlink)
        radarr = MagicMock()
        DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert _booked(conn) == [4]
        assert _action(conn) is None
        assert radarr.mock_calls == []

    def test_a_clear_once_a_part_removal_is_recorded_credits_its_bytes_once(
        self, conn, db_path, two_parts, monkeypatch
    ):
        other, real = init_db(str(db_path)), deletions._remove_bare_folders

        def clear_then_remove(*args):
            clear_pending_deletions(other)
            real(*args)

        monkeypatch.setattr(deletions, "_remove_bare_folders", clear_then_remove)
        _remove_all_but(conn, monkeypatch, "cd2.mkv")

        assert two_parts[1].exists()
        assert _booked(conn) == [4]


class TestRowsSharingAFolder:
    def test_a_row_part_removed_with_another_closes_later_in_the_same_pass(
        self, conn, root, monkeypatch
    ):
        outer, inner1, inner2 = (
            root / "Films" / "a.mkv",
            root / "Films" / "b" / "b1.mkv",
            root / "Films" / "c" / "b2.mkv",
        )
        _due(conn, "m1", str(root / "Films"), outer)
        _due(conn, "m2", str(inner1), inner1, inner2)
        real, locked = storage._unlink_regular_file, [inner2]

        def unlink(path, roots):
            if path in locked:
                locked.remove(path)
                raise PermissionError("busy")
            return real(path, roots)

        monkeypatch.setattr(storage, "_unlink_regular_file", unlink)
        DeletionExecutor(conn=conn).execute()

        assert not inner2.exists()
        assert _booked(conn, "m1") == [8]
        assert _booked(conn, "m2") == [4]
        assert _action(conn, "m2") is None
