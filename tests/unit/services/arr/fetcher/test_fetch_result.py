"""fetch_arr_queue_result: which services did not answer, and which refused their key."""

from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.services.arr.fetcher import fetch_arr_queue_result
from mediaman.services.infra import SafeHTTPError


def test_a_service_that_answers_401_refused_its_key_and_one_that_is_down_did_not(monkeypatch, conn):
    radarr, sonarr = MagicMock(), MagicMock()
    radarr.get_queue.side_effect = SafeHTTPError(401, "", "http://radarr:7878/api/v3/queue")
    sonarr.get_queue.side_effect = requests.ConnectionError("down")
    monkeypatch.setattr("mediaman.services.arr.build.build_radarr_from_db", lambda *a: radarr)
    monkeypatch.setattr("mediaman.services.arr.build.build_sonarr_from_db", lambda *a: sonarr)

    result = fetch_arr_queue_result(conn, "k" * 64)

    assert result.unreachable == ["Radarr", "Sonarr"]
    assert result.refused == ["Radarr"]


def _film(name: str, **overrides):
    return {
        "id": 1,
        "title": f"{name}.2021.1080p",
        "size": 1000,
        "sizeleft": 500,
        "status": "downloading",
        "movie": {"id": 1, "title": name, "year": 2021, "images": []},
        **overrides,
    }


def _serve(monkeypatch, radarr=None, sonarr=None):
    monkeypatch.setattr("mediaman.services.arr.build.build_radarr_from_db", lambda *a: radarr)
    monkeypatch.setattr("mediaman.services.arr.build.build_sonarr_from_db", lambda *a: sonarr)


@pytest.mark.parametrize(
    "bad",
    [
        _film("Bad", size="1000"),
        _film("Bad", movie="Bad"),
        _film("Bad", movie={"id": 2, "title": "Bad", "images": {"coverType": "poster"}}),
        _film("Bad", title=["Bad.2021"]),
    ],
    ids=["text size", "film not an object", "images not a list", "title a list"],
)
def test_a_malformed_radarr_record_is_left_out_and_the_others_kept(monkeypatch, conn, bad):
    radarr = MagicMock()
    radarr.get_queue.return_value = [bad, _film("Good")]
    radarr.get_movies.return_value = []
    _serve(monkeypatch, radarr=radarr)

    result = fetch_arr_queue_result(conn, "k" * 64)

    assert [card["title"] for card in result.cards] == ["Good"]
    assert result.unreachable == []


def test_a_malformed_sonarr_record_is_left_out_and_the_others_kept(monkeypatch, conn):
    good = {
        "id": 1,
        "title": "Alone.S01E01",
        "size": 10,
        "sizeleft": 0,
        "series": {"id": 5, "title": "Alone", "images": []},
        "episode": {"id": 50, "seasonNumber": 1, "episodeNumber": 1},
    }
    sonarr = MagicMock()
    sonarr.get_queue.return_value = [{**good, "series": ["Alone"]}, {**good, "episode": 3}, good]
    sonarr.get_series.return_value = []
    _serve(monkeypatch, sonarr=sonarr)

    result = fetch_arr_queue_result(conn, "k" * 64)

    assert [(card["title"], card["episode_count"]) for card in result.cards] == [("Alone", 1)]


def test_a_malformed_record_is_logged_once_while_it_stays(monkeypatch, conn, caplog):
    monkeypatch.setattr("mediaman.services.arr.fetcher._base._reported", set())
    radarr = MagicMock()
    radarr.get_queue.return_value = [_film("Bad", size="1000")]
    radarr.get_movies.return_value = []
    _serve(monkeypatch, radarr=radarr)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            fetch_arr_queue_result(conn, "k" * 64)

    assert sum("bad_records" in r.getMessage() for r in caplog.records) == 1


def test_a_service_failing_in_an_unforeseen_way_hides_only_its_own_cards(monkeypatch, conn):
    radarr, sonarr = MagicMock(), MagicMock()
    radarr.get_queue.side_effect = AttributeError("'str' object has no attribute 'get'")
    sonarr.get_queue.return_value = [
        {"id": 1, "title": "Alone.S01E01", "series": {"id": 5, "title": "Alone"}, "episode": {}}
    ]
    sonarr.get_series.return_value = []
    _serve(monkeypatch, radarr=radarr, sonarr=sonarr)

    result = fetch_arr_queue_result(conn, "k" * 64, steady=False)

    assert [card["title"] for card in result.cards] == ["Alone"]
    assert result.unreachable == ["Radarr"]


def test_a_poll_reads_radarr_and_sonarr_at_the_same_time(monkeypatch, conn):
    sonarr_asked = threading.Event()
    radarr, sonarr = MagicMock(), MagicMock()
    # Radarr answers only once Sonarr has been asked, as it would if Radarr hung.
    radarr.get_queue.side_effect = lambda **_: [] if sonarr_asked.wait(5) else None
    sonarr.get_queue.side_effect = lambda **_: sonarr_asked.set() or []
    radarr.get_movies.return_value = sonarr.get_series.return_value = []
    _serve(monkeypatch, radarr=radarr, sonarr=sonarr)

    # Not steady: no last answer may stand in for a Radarr that failed.
    result = fetch_arr_queue_result(conn, "k" * 64, steady=False)

    assert result.unreachable == []


def test_a_poll_gives_each_queue_one_short_try(monkeypatch, conn):
    radarr = MagicMock()
    radarr.get_queue.return_value = []
    radarr.get_movies.return_value = []
    _serve(monkeypatch, radarr=radarr)

    fetch_arr_queue_result(conn, "k" * 64)

    radarr.get_queue.assert_called_once_with(quick=True)
