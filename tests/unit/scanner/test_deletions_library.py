"""Tests that the due deletions of a library left out of Settings are cancelled."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.scanner import deletions
from mediaman.scanner.deletions import DeletionExecutor
from mediaman.scanner.phases.upsert import schedule_deletion
from mediaman.scanner.repository import is_already_scheduled
from tests.helpers.factories import insert_media_item, insert_scheduled_action


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
    return root


def _select(conn, value):
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) "
        "VALUES ('plex_libraries', ?, '2026')",
        (value,),
    )
    conn.commit()


def _due(conn, media_id, file, *, library=1, path=None):
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b"data")
    insert_media_item(
        conn,
        id=media_id,
        title=media_id,
        plex_rating_key=media_id,
        plex_library_id=library,
        file_path=str(path or file),
    )
    conn.execute(
        "UPDATE media_items SET tracked_files = ? WHERE id = ?",
        (json.dumps([{"path": str(file), "size": 4}]), media_id),
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
    return file


def _status(conn, media_id):
    row = conn.execute(
        "SELECT delete_status FROM scheduled_actions WHERE media_item_id = ?", (media_id,)
    ).fetchone()
    return row[0] if row else None


def _audits(conn, media_id):
    return [
        r[0]
        for r in conn.execute(
            "SELECT action FROM audit_log WHERE media_item_id = ? ORDER BY id", (media_id,)
        )
    ]


@pytest.mark.parametrize("selected", [None, "", '["2"]', "[]", "not json", '{"1": "1"}'])
def test_a_due_deletion_of_an_unselected_library_is_cancelled(conn, root, selected):
    film = _due(conn, "m1", root / "Film" / "film.mkv")
    if selected is None:
        conn.execute("DELETE FROM settings WHERE key = 'plex_libraries'")
        conn.commit()
    else:
        _select(conn, selected)

    for _ in range(2):
        DeletionExecutor(conn=conn).execute()

    assert film.exists()
    assert _status(conn, "m1") is None
    assert _audits(conn, "m1") == ["delete_refused"]


@pytest.mark.parametrize("selected", ['["1"]', "[1]", '["2", "1"]'])
def test_a_due_deletion_of_a_selected_library_goes_ahead(conn, root, selected):
    film = _due(conn, "m1", root / "Film" / "film.mkv")
    _select(conn, selected)

    DeletionExecutor(conn=conn).execute()

    assert not film.exists()


def test_a_library_selected_again_starts_with_a_new_schedule(conn, root):
    film = _due(conn, "m1", root / "Film" / "film.mkv")
    _select(conn, "[]")
    DeletionExecutor(conn=conn).execute()
    _select(conn, '["1"]')

    DeletionExecutor(conn=conn).execute()
    assert film.exists()

    assert not is_already_scheduled(conn, "m1")
    schedule_deletion(conn, media_id="m1", is_reentry=False, grace_days=14, secret_key="k" * 64)
    DeletionExecutor(conn=conn).execute()
    assert film.exists()


def test_a_part_removed_deletion_of_an_unselected_library_keeps_its_record(conn, root):
    film = _due(conn, "m1", root / "Film" / "film.mkv")
    removed = json.dumps({"paths": [str(root / "gone.mkv")], "bytes": 4})
    conn.execute("UPDATE scheduled_actions SET removed_files = ?", (removed,))
    _select(conn, "[]")

    for _ in range(2):
        DeletionExecutor(conn=conn).execute()

    assert film.exists()
    row = conn.execute("SELECT delete_status, removed_files FROM scheduled_actions").fetchone()
    assert tuple(row) == ("pending", removed)
    assert _audits(conn, "m1") == ["delete_refused"]


def test_a_dry_run_does_not_list_a_held_deletion(conn, root):
    _due(conn, "m1", root / "Film" / "film.mkv")
    _select(conn, "[]")

    DeletionExecutor(conn=conn, dry_run=True, cleanup_snoozes=False).execute()

    assert _audits(conn, "m1") == []


def test_an_unselected_librarys_film_inside_a_due_folder_holds_the_folder_back(conn, root):
    folder = root / "Films"
    outer = _due(conn, "m1", folder / "a.mkv", path=folder)
    inner = _due(conn, "m2", folder / "b" / "b.mkv", library=2)
    _select(conn, '["1"]')

    DeletionExecutor(conn=conn).execute()

    assert outer.exists()
    assert inner.exists()


def test_a_library_unselected_after_the_claim_keeps_its_files(conn, db_path, root, monkeypatch):
    film = _due(conn, "m1", root / "Film" / "film.mkv")
    _select(conn, '["1"]')
    other, real = init_db(str(db_path)), deletions._new_copy

    def unselect(row):
        if _status(other, "m1") == "deleting":
            _select(other, "[]")
        return real(row)

    monkeypatch.setattr(deletions, "_new_copy", unselect)
    DeletionExecutor(conn=conn).execute()

    assert film.exists()
    assert _status(conn, "m1") == "pending"


def test_a_removal_finished_before_its_library_was_unselected_is_booked(conn, root, monkeypatch):
    _due(conn, "m1", root / "Film" / "film.mkv")
    with monkeypatch.context() as m:
        m.setattr(deletions, "_remove_bare_folders", MagicMock(side_effect=_Died))
        with pytest.raises(_Died):
            DeletionExecutor(conn=conn).execute()
    deletions.recover_stuck_deletions(conn, book=False)
    _select(conn, "[]")

    DeletionExecutor(conn=conn).execute()

    assert _status(conn, "m1") is None
    assert _audits(conn, "m1") == ["deleted"]
