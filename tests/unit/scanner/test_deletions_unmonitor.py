"""Tests that Radarr/Sonarr is told of a booked deletion even when the run dies first."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.core.audit import log_audit
from mediaman.core.time import now_utc
from mediaman.db import init_db
from mediaman.scanner import deletions, runner
from mediaman.scanner.deletions import DeletionExecutor
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.services.infra import ConfigDecryptError, SafeHTTPError
from tests.helpers.factories import insert_media_item, insert_scheduled_action, insert_settings


class _Died(BaseException):
    pass


_NO_ANSWER = requests.ConnectionError("refused")


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


def _rescanned(radarr):
    return [call.args[0] for call in radarr.rescan_movie.call_args_list]


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

    assert _unmonitored(radarr) == []
    assert _rescanned(radarr) == [1]
    assert _skips(conn) == ["m1"]


def test_a_season_asked_for_again_has_its_show_rescanned_and_stays_monitored(conn, root):
    _due(conn, root, 1)
    conn.execute(
        "UPDATE media_items SET media_type = 'tv_season', season_number = 1, sonarr_id = 7, "
        "tvdb_id = 9, tmdb_id = NULL"
    )
    conn.commit()
    dying = MagicMock()
    dying.get_series_by_id.side_effect = _Died
    with pytest.raises(_Died):
        DeletionExecutor(conn=conn, sonarr_client=dying).execute()
    log_audit(conn, "tvdb:9:s1", "re_downloaded", "again")
    conn.commit()
    sonarr = MagicMock()
    sonarr.get_series_by_id.return_value = {"id": 7, "tvdbId": 9}

    DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

    sonarr.rescan_series.assert_called_once_with(7)
    assert not sonarr.unmonitor_season.called


def test_a_title_asked_for_again_is_rescanned_once_radarr_answers(conn, root, monkeypatch):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    log_audit(conn, "tmdb:601", "re_downloaded", "again")
    conn.commit()
    down = _radarr(root)
    down.get_movie_by_id.side_effect = requests.ConnectionError("down")
    _sync(conn, monkeypatch, down)
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert _rescanned(radarr) == [1]
    assert _skips(conn) == ["m1"]


def _ask(conn, service, *, tmdb_id=None, tvdb_id=None, season_number=None, email="a@b.c"):
    """Record a download as the search page does, under the bare title "Film"."""
    log_audit(conn, "Film", "downloaded", f"Added 'Film' to {service} by admin")
    if email:
        record_download_notification(
            conn,
            email=email,
            title="Film",
            media_type="movie" if service == "Radarr" else "tv",
            service=service.lower(),
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            season_number=season_number,
        )
    conn.commit()


@pytest.mark.parametrize(
    "request_",
    [
        {"service": "Sonarr", "tvdb_id": 5},
        {"service": "Sonarr", "email": None},
        {"service": "Radarr", "tmdb_id": 999},
    ],
    ids=["a show", "a show, no email", "another movie"],
)
def test_another_title_of_the_same_name_asked_for_does_not_stop_the_unmonitor(
    conn, root, monkeypatch, request_
):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    _ask(conn, **request_)
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert _unmonitored(radarr) == [1]


@pytest.mark.parametrize("others", [0, 1])
def test_the_movie_itself_asked_for_by_search_stays_monitored(conn, root, monkeypatch, others):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    for _ in range(others):
        _ask(conn, "Radarr", tmdb_id=999)
    _ask(conn, "Radarr", tmdb_id=601)
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert _unmonitored(radarr) == []


def _notify_by_email(conn, tmdb_id):
    record_download_notification(
        conn,
        email="a@b.c",
        title="Film",
        media_type="movie",
        service="radarr",
        tmdb_id=tmdb_id,
    )


def _ask_by_email(conn, tmdb_id, *, notified=True):
    """Record a download as an emailed link does, under the bare title "Film"."""
    log_audit(conn, "Film", "downloaded", "Downloaded 'Film' by a@b.c")
    if notified:
        _notify_by_email(conn, tmdb_id)
    conn.commit()


def test_another_film_of_the_same_name_asked_for_by_email_does_not_stop_the_unmonitor(
    conn, root, monkeypatch
):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    _ask_by_email(conn, 999)
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert _unmonitored(radarr) == [1]


def _ask_by_email_unrecorded(conn):
    _ask_by_email(conn, 601, notified=False)


def _ask_by_search_unrecorded(conn):
    _ask(conn, "Radarr", email=None)


@pytest.mark.parametrize("wanted_first", [True, False])
@pytest.mark.parametrize("ask_wanted", [_ask_by_email_unrecorded, _ask_by_search_unrecorded])
def test_a_request_no_notification_tells_of_keeps_the_title_monitored_beside_another_films(
    conn, root, monkeypatch, ask_wanted, wanted_first
):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    if wanted_first:
        ask_wanted(conn)
    _ask_by_email(conn, 999)
    if not wanted_first:
        ask_wanted(conn)
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert _unmonitored(radarr) == []


def test_a_notification_of_another_film_recorded_at_another_time_does_not_tell_of_a_request(
    conn, root, monkeypatch
):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    _notify_by_email(conn, 999)
    conn.execute(
        "UPDATE download_notifications SET created_at = ?",
        ((now_utc() + timedelta(hours=1)).isoformat(),),
    )
    _ask_by_email(conn, 601, notified=False)
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert _unmonitored(radarr) == []


def test_a_film_whose_add_got_no_answer_and_only_its_email_is_recorded_stays_monitored(
    conn, root, monkeypatch
):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    _notify_by_email(conn, 601)
    conn.commit()
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert _unmonitored(radarr) == []


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
        m.setattr(deletions, "await_rescan", MagicMock(side_effect=sqlite3.OperationalError))
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
    assert _skips(conn) == ["m1"]
    assert conn.execute("SELECT 1 FROM pending_unmonitors").fetchone()


def test_an_unreadable_radarr_key_keeps_its_titles_marked_and_sonarrs_told(conn, root, monkeypatch):
    for number in (1, 2):
        _due(conn, root, number)
    conn.execute(
        "UPDATE media_items SET media_type = 'tv_season', season_number = 1, sonarr_id = 7, "
        "tvdb_id = 9, tmdb_id = NULL WHERE id = 'm2'"
    )
    conn.commit()
    dying = MagicMock()
    dying.get_movie_by_id.side_effect = dying.get_series_by_id.side_effect = _NO_ANSWER
    DeletionExecutor(conn=conn, radarr_client=dying, sonarr_client=dying).execute()
    sonarr, radarr = MagicMock(), _radarr(root)
    sonarr.get_series_by_id.return_value = {
        "id": 7,
        "tvdbId": 9,
        "path": str(root),
        "seasons": [{"seasonNumber": 1}],
    }
    sonarr.get_episode_files.return_value = []
    monkeypatch.setattr(runner, "_build_sonarr", lambda *a: sonarr)
    with monkeypatch.context() as m:
        m.setattr(runner, "_build_plex_client", lambda *a: None)
        m.setattr(
            runner,
            "_build_radarr",
            MagicMock(side_effect=ConfigDecryptError("radarr_api_key", ValueError())),
        )
        runner.run_library_sync(conn, "secret")
    sonarr.unmonitor_season.assert_called_once_with(7, 1)

    _sync(conn, monkeypatch, radarr)

    assert _unmonitored(radarr) == [1]


@pytest.mark.parametrize(
    "failure",
    [
        _NO_ANSWER,
        requests.ReadTimeout("slow"),
        SafeHTTPError(0, "transport error", "u"),
        SafeHTTPError(401, "wrong key", "u"),
        SafeHTTPError(503, "", "u"),
    ],
)
def test_a_radarr_that_gives_no_answer_is_audited_once_and_told_once_it_does(conn, root, failure):
    _due(conn, root, 1)
    down = _radarr(root)
    down.get_movie_by_id.side_effect = failure
    for _ in range(2):
        DeletionExecutor(conn=conn, radarr_client=down).execute()
    assert _skips(conn) == ["m1"]
    radarr = _radarr(root)

    DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert _unmonitored(radarr) == [1]
    assert _skips(conn) == ["m1"]


def _caused_by(error, cause):
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    "failure",
    [
        _NO_ANSWER,
        _caused_by(SafeHTTPError(0, "transport error", "u"), requests.ConnectTimeout("no host")),
        SafeHTTPError(403, "wrong key", "u"),
    ],
    ids=["refused", "no host", "wrong key"],
)
def test_a_radarr_that_is_down_is_waited_for_once_a_pass(conn, root, failure):
    for number in (1, 2, 3):
        _due(conn, root, number)
    down = _radarr(root)
    down.get_movie_by_id.side_effect = failure

    DeletionExecutor(conn=conn, radarr_client=down).execute()

    assert len(down.mock_calls) == 1
    assert _skips(conn) == ["m1", "m2", "m3"]


_SLOW = _caused_by(SafeHTTPError(0, "transport error", "u"), requests.ReadTimeout("slow"))


def _radarr_stuck_on(root, *stuck, failure=_SLOW):
    radarr = _radarr(root)
    answer = radarr.get_movie_by_id.side_effect
    radarr.get_movie_by_id.side_effect = lambda i: _raise(failure) if i in stuck else answer(i)
    return radarr


def _asked(radarr):
    return [call.args[0] for call in radarr.get_movie_by_id.call_args_list]


@pytest.mark.parametrize(
    "failure", [_SLOW, requests.ReadTimeout("slow"), SafeHTTPError(504, "", "u")]
)
def test_a_title_radarr_never_answers_for_does_not_hold_up_the_others(conn, root, failure):
    for number in (1, 2, 3):
        _due(conn, root, number)
    radarr = _radarr_stuck_on(root, 1, failure=failure)

    DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert _unmonitored(radarr) == [2, 3]
    assert _asked(radarr).count(1) == 1
    assert _skips(conn) == ["m1"]


def test_a_pass_that_waited_long_enough_asks_about_no_more_titles(conn, root, monkeypatch):
    for number in (1, 2, 3, 4):
        _due(conn, root, number)
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(deletions, "monotonic", lambda: next(clock))
    radarr = _radarr_stuck_on(root, 1, 2, 3, 4)

    DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert _asked(radarr) == [1, 2]


def test_the_titles_given_no_answer_are_asked_about_last_the_next_time(conn, root, monkeypatch):
    for number in (1, 2, 3, 4):
        _due(conn, root, number)
    down = _radarr_stuck_on(root, 1, 2, 3, 4)
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(deletions, "monotonic", lambda: next(clock))
    DeletionExecutor(conn=conn, radarr_client=down).execute()
    radarr = _radarr_stuck_on(root, 1, 2)

    _sync(conn, monkeypatch, radarr)

    assert _unmonitored(radarr) == [3, 4]
    assert _asked(radarr) == [3, 4, 1, 2]


def _pass_days(monkeypatch, days):
    later = now_utc() + timedelta(days=days)
    monkeypatch.setattr(deletions, "now_utc", lambda: later)


def _marks(conn):
    return conn.execute("SELECT COUNT(*) FROM pending_unmonitors").fetchone()[0]


@pytest.mark.parametrize(("days", "unmonitored", "skips"), [(6, [1], []), (8, [], ["m1"])])
def test_a_title_is_left_as_it_is_once_radarr_was_out_of_reach_for_a_week(
    conn, root, monkeypatch, days, unmonitored, skips
):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    _pass_days(monkeypatch, days)
    radarr = _radarr(root)

    _sync(conn, monkeypatch, radarr)

    assert _unmonitored(radarr) == unmonitored
    assert _marks(conn) == 0
    assert _skips(conn) == skips


def test_a_mark_whose_lookup_always_fails_goes_after_a_week_with_one_audit(conn, root, monkeypatch):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    log_audit(conn, "tmdb:601", "re_downloaded", "again")
    conn.commit()
    radarr = _radarr_stuck_on(root, 1, failure=ValueError("not json"))
    _sync(conn, monkeypatch, radarr)
    assert _marks(conn) == 1
    _pass_days(monkeypatch, 8)

    for _ in range(2):
        _sync(conn, monkeypatch, radarr)

    assert _marks(conn) == 0
    assert _skips(conn) == ["m1"]


def test_a_radarr_whose_key_cannot_be_read_for_a_week_has_its_marks_dropped(
    conn, root, monkeypatch
):
    _due(conn, root, 1)
    _die_unmonitoring(conn, root)
    _pass_days(monkeypatch, 8)
    monkeypatch.setattr(runner, "_build_plex_client", lambda *a: None)
    monkeypatch.setattr(
        runner,
        "_build_radarr",
        MagicMock(side_effect=ConfigDecryptError("radarr_api_key", ValueError())),
    )

    runner.run_library_sync(conn, "secret")

    assert _marks(conn) == 0
    assert _skips(conn) == ["m1"]


def test_a_rescan_not_asked_for_within_a_week_is_given_up_with_one_audit(conn, root, monkeypatch):
    _due(conn, root, 1)
    radarr = _radarr(root)
    radarr.rescan_movie.side_effect = _SLOW
    DeletionExecutor(conn=conn, radarr_client=radarr).execute()
    _pass_days(monkeypatch, 8)

    for _ in range(2):
        _sync(conn, monkeypatch, radarr)

    assert _rescanned(radarr) == [1]
    assert _marks(conn) == 0
    assert _skips(conn) == ["m1"]


def test_a_radarr_that_refuses_the_unmonitor_is_not_asked_again(conn, root):
    _due(conn, root, 1)
    refusing = _radarr(root)
    refusing.unmonitor_movie.side_effect = SafeHTTPError(400, "", "u")
    DeletionExecutor(conn=conn, radarr_client=refusing).execute()
    radarr = _radarr(root)

    DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert radarr.mock_calls == []
    assert _skips(conn) == ["m1"]


def test_marks_left_for_a_radarr_no_longer_set_up_go_unaudited(conn, root, monkeypatch):
    for number in (1, 2):
        _due(conn, root, number)
    radarr = _radarr(root)
    answer = radarr.get_movie_by_id.side_effect
    radarr.get_movie_by_id.side_effect = lambda i: answer(i) if i == 1 else _raise(_Died())
    with pytest.raises(_Died):
        DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    _sync(conn, monkeypatch, None)

    assert not conn.execute("SELECT 1 FROM pending_unmonitors").fetchone()
    assert _skips(conn) == []


def _raise(error):
    raise error


def test_an_older_database_without_the_table_gets_it(conn, db_path, root):
    _due(conn, root, 1)
    conn.execute("DROP TABLE pending_unmonitors")
    conn.commit()
    radarr = _radarr(root)

    DeletionExecutor(conn=init_db(str(db_path)), radarr_client=radarr).execute()

    assert _unmonitored(radarr) == [1]


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


@pytest.mark.parametrize(
    ("failure", "asked"),
    [(_NO_ANSWER, [1]), (_SLOW, [1, 2]), (SafeHTTPError(503, "", "u"), [1, 2])],
)
def test_a_rescan_that_fails_leaves_the_deletion_booked_and_ends_the_turn_of_a_radarr_that_is_down(
    conn, root, failure, asked
):
    for number in (1, 2):
        _due(conn, root, number)
    radarr = _radarr(root)
    radarr.rescan_movie.side_effect = failure

    result = DeletionExecutor(conn=conn, radarr_client=radarr).execute()

    assert result["deleted"] == 2
    assert _rescanned(radarr) == asked
    assert not conn.execute("SELECT 1 FROM audit_log WHERE action = 'unmonitor_skipped'").fetchone()


@pytest.mark.parametrize(
    "failure", [_Died(), KeyError("id"), requests.ReadTimeout("slow"), SafeHTTPError(503, "", "u")]
)
def test_a_rescan_that_was_not_asked_for_is_asked_for_by_the_next_sync_once(
    conn, root, monkeypatch, failure
):
    _due(conn, root, 1)
    radarr = _radarr(root)
    radarr.rescan_movie.side_effect = failure
    with contextlib.suppress(_Died):
        DeletionExecutor(conn=conn, radarr_client=radarr).execute()
    conn.rollback()
    radarr = _radarr(root)

    for _ in range(2):
        _sync(conn, monkeypatch, radarr)

    assert _rescanned(radarr) == [1]
    assert _unmonitored(radarr) == []


def test_an_entry_radarr_refuses_to_rescan_does_not_hold_up_the_others(conn, root, monkeypatch):
    for number in (1, 2):
        _due(conn, root, number)
    radarr = _radarr(root)
    radarr.rescan_movie.side_effect = [SafeHTTPError(404, "", "u"), None]

    DeletionExecutor(conn=conn, radarr_client=radarr).execute()
    _sync(conn, monkeypatch, radarr)

    assert _rescanned(radarr) == [1, 2]
