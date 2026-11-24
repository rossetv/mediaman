"""Tests that Radarr/Sonarr is told of a booked deletion even when the run dies first."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.core.audit import log_audit
from mediaman.db import init_db
from mediaman.scanner import deletions, runner
from mediaman.scanner.deletions import DeletionExecutor
from mediaman.services.infra import ConfigDecryptError
from tests.helpers.factories import insert_media_item, insert_scheduled_action, insert_settings


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


def _due(conn, root, number):
    file = root / f"Film {number}" / "film.mkv"
    file.parent.mkdir()
    file.write_bytes(b"data")
    media_id = f"m{number}"
    insert_media_item(
        conn, id=media_id, title="Film", plex_rating_key=media_id, file_path=str(file)
    )
    conn.execute(
        "UPDATE media_items SET tmdb_id = ?, radarr_id = ?, tracked_files = ? WHERE id = ?",
        (600 + number, number, json.dumps([{"path": str(file), "size": 4}]), media_id),
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


def _radarr(root):
    radarr = MagicMock()
    radarr.get_movie_by_id.side_effect = lambda radarr_id: {
        "id": radarr_id,
        "tmdbId": 600 + radarr_id,
        "path": str(root / f"Film {radarr_id}"),
    }
    return radarr


def _unmonitored(radarr):
    return [call.args[0] for call in radarr.unmonitor_movie.call_args_list]


def _die_unmonitoring(conn, root):
    radarr = _radarr(root)
    radarr.get_movie_by_id.side_effect = _Died
    with pytest.raises(_Died):
        DeletionExecutor(conn=conn, radarr_client=radarr).execute()
    conn.rollback()
    assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'deleted'").fetchone()


def _skips(conn):
    return [
        row[0]
        for row in conn.execute(
            "SELECT media_item_id FROM audit_log WHERE action = 'unmonitor_skipped' ORDER BY id"
        )
    ]


def _sync(conn, monkeypatch, radarr):
    monkeypatch.setattr(runner, "_build_radarr", lambda *a: radarr)
    monkeypatch.setattr(runner, "_build_plex_client", lambda *a: None)
    runner.run_library_sync(conn, "secret")


def test_a_run_that_died_before_unmonitoring_is_finished_by_the_next_pass_once(conn, root):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    radarr = _radarr(root)

    for _ in range(2):
        DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert _unmonitored(radarr) == [1]


def test_the_next_library_sync_finishes_it_too(conn, root, monkeypatch):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    radarr = _radarr(root)

    for _ in range(2):
        _sync(conn, monkeypatch, radarr)

    assert _unmonitored(radarr) == [1]


def test_a_dry_run_leaves_it_for_a_live_pass(conn, root):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    radarr = _radarr(root)

    DeletionExecutor(conn=conn, dry_run=True, cleanup_snoozes=False, radarr_client=radarr).execute()

    assert radarr.mock_calls == []


def test_a_library_sync_in_dry_run_leaves_it_for_a_live_one(conn, root, monkeypatch):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    insert_settings(conn, dry_run="true")
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)
    assert radarr.mock_calls == []

    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'dry_run'")
    _sync(conn, monkeypatch, radarr)
    assert _unmonitored(radarr) == [1]


def test_a_recovery_that_fails_midway_still_gets_its_booked_rows_unmonitored(
    conn, root, monkeypatch
):
    for number in (1, 2):
        file = _due(conn, root, number)
        file.unlink()
        conn.execute(
            "UPDATE scheduled_actions SET delete_status = 'deleting', removed_files = ? "
            "WHERE media_item_id = ?",
            (json.dumps({"paths": [str(file)], "bytes": 4}), f"m{number}"),
        )
    conn.commit()
    real, calls = deletions.book_deletion, []

    def flaky(conn, row, *args, **kwargs):
        calls.append(row.media_item_id)
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")
        real(conn, row, *args, **kwargs)

    radarr = _radarr(root)
    with monkeypatch.context() as m:
        m.setattr(deletions, "book_deletion", flaky)
        with pytest.raises(sqlite3.OperationalError):
            DeletionExecutor(conn=conn, radarr_client=radarr).execute()
    conn.rollback()
    DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert _unmonitored(radarr) == [1, 2]


def test_a_title_on_disk_again_stays_monitored(conn, root, monkeypatch):
    file = _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    file.parent.mkdir(exist_ok=True)
    file.write_bytes(b"a new copy")
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert radarr.mock_calls == []


@pytest.mark.parametrize(("key", "action"), [("Film", "downloaded"), ("tmdb:601", "re_downloaded")])
def test_a_title_asked_for_again_stays_monitored(conn, root, monkeypatch, key, action):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    log_audit(conn, key, action, "again")
    conn.commit()
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert radarr.mock_calls == []


def test_a_title_downloaded_only_before_its_deletion_is_unmonitored(conn, root):
    _due(conn, root, 1)
    log_audit(conn, "tmdb:601", "re_downloaded", "before")
    conn.commit()
    radarr = _radarr(root)

    DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert _unmonitored(radarr) == [1]


@pytest.mark.parametrize("error", [KeyError("id"), AttributeError("get"), TypeError("str")])
def test_a_row_that_fails_unexpectedly_is_audited_and_the_rest_go_on(conn, root, error):
    files = [_due(conn, root, number) for number in (1, 2)]
    radarr = _radarr(root)
    answer = radarr.get_movie_by_id.side_effect

    def fail_first(radarr_id):
        if radarr_id == 1:
            raise error
        return answer(radarr_id)

    radarr.get_movie_by_id.side_effect = fail_first

    DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert [f.exists() for f in files] == [False, False]
    assert _unmonitored(radarr) == [2]
    assert _skips(conn) == ["m1"]
    assert not conn.execute("SELECT 1 FROM pending_unmonitors").fetchone()


def test_a_database_error_while_unmonitoring_leaves_no_due_row_undeleted(conn, root, monkeypatch):
    files = [_due(conn, root, number) for number in (1, 2)]
    with monkeypatch.context() as m:
        m.setattr(deletions, "finish_unmonitor", MagicMock(side_effect=sqlite3.OperationalError))
        DeletionExecutor(conn=conn, radarr_client=_radarr(root)).execute()

    assert [f.exists() for f in files] == [False, False]
    assert len(conn.execute("SELECT 1 FROM pending_unmonitors").fetchall()) == 2


def test_an_unreadable_radarr_key_does_not_stop_the_library_sync(conn, root, monkeypatch):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    plex = MagicMock(return_value=None)
    monkeypatch.setattr(
        runner,
        "_build_radarr",
        MagicMock(side_effect=ConfigDecryptError("radarr_api_key", ValueError())),
    )
    monkeypatch.setattr(runner, "_build_plex_client", plex)

    runner.run_library_sync(conn, "secret")

    assert plex.called


def test_an_older_database_without_the_table_gets_it(conn, db_path, root):
    _due(conn, root, 1)
    conn.execute("DROP TABLE pending_unmonitors")
    conn.commit()
    radarr = _radarr(root)

    DeletionExecutor(conn=init_db(str(db_path)), radarr_client=radarr).execute()

    assert _unmonitored(radarr) == [1]


def _rescanned(radarr):
    return [call.args[0] for call in radarr.rescan_movie.call_args_list]


def test_radarr_is_asked_to_rescan_every_deleted_title_once_the_deletions_are_done(conn, root):
    files = [_due(conn, root, number) for number in (1, 2)]
    radarr = _radarr(root)
    left = []
    radarr.rescan_movie.side_effect = lambda _id: left.extend(f for f in files if f.exists())

    DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert _rescanned(radarr) == [1, 2]
    assert left == []


def test_the_library_sync_that_finishes_a_dead_run_asks_for_the_rescan(conn, root, monkeypatch):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert _rescanned(radarr) == [1]


def test_sonarr_is_asked_to_rescan_a_show_once_for_two_seasons(conn, root):
    for number in (1, 2):
        _due(conn, root, number)
        conn.execute(
            "UPDATE media_items SET media_type = 'tv_season', season_number = ?, sonarr_id = 7, "
            "tvdb_id = 9, tmdb_id = NULL WHERE id = ?",
            (number, f"m{number}"),
        )
    conn.commit()
    sonarr = MagicMock()
    sonarr.get_series_by_id.return_value = {
        "id": 7,
        "tvdbId": 9,
        "path": str(root),
        "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
    }
    sonarr.get_episode_files.return_value = []

    DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

    sonarr.rescan_series.assert_called_once_with(7)


def test_a_title_left_monitored_is_not_rescanned(conn, root):
    _due(conn, root, 1)
    radarr = _radarr(root)
    radarr.unmonitor_movie.side_effect = requests.ConnectionError("down")

    DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert _rescanned(radarr) == []


def test_a_rescan_that_fails_leaves_the_deletion_booked_and_is_not_repeated(conn, root):
    for number in (1, 2):
        _due(conn, root, number)
    radarr = _radarr(root)
    radarr.rescan_movie.side_effect = requests.ReadTimeout("slow")

    result = DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert result["deleted"] == 2
    assert _rescanned(radarr) == [1]
    assert not conn.execute("SELECT 1 FROM audit_log WHERE action = 'unmonitor_skipped'").fetchone()
