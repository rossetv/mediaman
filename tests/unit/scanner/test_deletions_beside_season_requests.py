"""A deletion beside somebody asking Sonarr for seasons of the same show."""

# ruff: noqa: F811

from __future__ import annotations

import copy
import threading

import pytest

from mediaman.scanner.deletions import DeletionExecutor
from mediaman.services.arr import _sonarr_methods
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.spec import SONARR_SPEC
from tests.unit.scanner.test_deletions_arr_and_roots import (  # noqa: F401
    _SERIES,
    _audit,
    conn,
    season,
)


@pytest.fixture
def client():
    return ArrClient(SONARR_SPEC, "http://sonarr:8989", "key")


@pytest.fixture
def series(fake_http, fake_response):
    """The series of the deleted season, in a Sonarr that does what it is asked."""
    held = {**copy.deepcopy(_SERIES), "seasons": [{"seasonNumber": 1, "monitored": True}]}

    def arr(method, url, **kwargs):
        if method == "PUT":
            held.update(copy.deepcopy(kwargs["json"]))
        listing = "episodefile" in url or "/episode?" in url
        return fake_response(json_data=[] if listing else copy.deepcopy(held))

    fake_http.handler(arr)
    return held


def _marks(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM pending_unmonitors"
        " WHERE json_extract(removed_files, '$.rescan') IS NULL"
    ).fetchone()[0]


def test_a_request_that_holds_the_series_does_not_leave_a_deleted_season_monitored(
    conn, season, client, series, monkeypatch
):
    monkeypatch.setattr(_sonarr_methods, "_TURN_SECONDS", 0.05)
    key = ("http://sonarr:8989", series["id"])
    with _sonarr_methods._series_locks.setdefault(key, threading.Lock()):
        DeletionExecutor(conn=conn, sonarr_client=client).execute()

    assert series["seasons"][0]["monitored"] is False
    assert _audit(conn, "s1", "unmonitor_skipped") == []


@pytest.mark.parametrize("what", ["no time left", "no turn"])
def test_a_deletion_that_ran_out_of_time_is_told_again_the_next_time(
    conn, season, client, series, monkeypatch, what
):
    monkeypatch.setattr(_sonarr_methods, "_STEP_SECONDS", 0.05)
    key = ("http://sonarr:8989", series["id"])
    lock = _sonarr_methods._series_locks.setdefault(key, threading.Lock())
    with lock, client.within(40 if what == "no turn" else -1):
        DeletionExecutor(conn=conn, sonarr_client=client).execute()

    assert (series["seasons"][0]["monitored"], _marks(conn)) == (True, 1)

    DeletionExecutor(conn=conn, sonarr_client=client).execute()

    assert (series["seasons"][0]["monitored"], _marks(conn)) == (False, 0)
