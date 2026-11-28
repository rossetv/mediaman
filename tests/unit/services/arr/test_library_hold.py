"""The Downloads poll reads Radarr's and Sonarr's whole libraries once per hold, not per poll."""

from __future__ import annotations

import copy
from collections import Counter
from urllib.parse import urlsplit

import pytest

from mediaman.services.arr import _library_hold
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.spec import RADARR_SPEC
from mediaman.services.downloads.download_queue import build_downloads_response
from tests.conftest import _fake_response
from tests.unit.services.arr.test_import_retry import (  # noqa: F401  (fixtures)
    _KEY,
    _downloading,
    _stuck_film,
    arrs,
)

_SEARCHING_FILM = {
    "id": 9,
    "title": "Dune",
    "year": 2021,
    "tmdbId": 438631,
    "hasFile": False,
    "monitored": True,
    "images": [],
}
_SEARCHING_SHOW = {
    "id": 7,
    "title": "Severance",
    "year": 2022,
    "tmdbId": 95396,
    "monitored": True,
    "statistics": {"episodeFileCount": 0},
    "images": [],
}
_QUEUED_FILM = {"id": 8, "title": "Arrival", "year": 2016, "tmdbId": 329865, "images": []}
_LIBRARY = ("/api/v3/movie", "/api/v3/series", "/api/v3/episode")


@pytest.fixture
def library(arrs, fake_http, monkeypatch):  # noqa: F811  (the fixture of that name)
    """Arrs with a film downloading, another film and a show still searching, a clock for the
    hold, and the library reads counted by path."""
    arrs.queues["radarr"] = [_downloading(_stuck_film(3, movie=_QUEUED_FILM))]
    arrs.movies = [_SEARCHING_FILM]
    clock = [1000.0]
    monkeypatch.setattr(_library_hold, "clock", lambda: clock[0])
    reads: Counter[str] = Counter()
    inner = arrs._answer

    def answer(method, url, **kwargs):
        path = urlsplit(url).path
        if method == "GET" and path in _LIBRARY:
            reads[path] += 1
        if path == "/api/v3/series":
            return _fake_response(json_data=[_SEARCHING_SHOW])
        if path == "/api/v3/episode" and "seriesId=7" in url:
            return _fake_response(json_data=[])
        if path == "/api/v3/movie":
            # Copies, as over the wire: what is held is never changed under it.
            return _fake_response(json_data=copy.deepcopy(arrs.movies))
        if method == "PUT" and path == "/api/v3/movie/9":
            arrs.movies = [kwargs["json"]]
            return _fake_response(json_data=kwargs["json"])
        return inner(method, url, **kwargs)

    fake_http.handler(answer)
    arrs.clock, arrs.library_reads = clock, reads
    return arrs


def _poll(held, *, steady: bool = True) -> dict:
    return build_downloads_response(held.conn, _KEY, steady=steady)


def _shown(answer: dict) -> set[str]:
    """The titles a poll shows, downloading, searching or coming."""
    items = [answer["hero"], *answer["queue"], *answer["upcoming"]]
    return {item["title"] for item in items if item}


def test_polls_within_the_hold_read_each_library_list_once(library):
    for _ in range(3):
        shown = _shown(_poll(library))

    assert library.library_reads == dict.fromkeys(_LIBRARY, 1)
    assert shown == {"Arrival", "Dune", "Severance"}


def test_a_change_to_the_queue_reads_the_library_again(library):
    _poll(library)
    library.queues["radarr"] = []
    library.movies = [{**_SEARCHING_FILM, "hasFile": True}]

    titles = _shown(_poll(library))

    assert "Dune" not in titles


def test_a_write_to_the_service_reads_the_library_again(library):
    _poll(library)
    library.movies = [{**_SEARCHING_FILM, "monitored": False}]
    ArrClient(RADARR_SPEC, "http://radarr.test:7878", "k").send_command({"name": "RefreshMovie"})

    titles = _shown(_poll(library))

    assert library.library_reads["/api/v3/movie"] == 2
    assert "Dune" not in titles


def test_a_film_abandoned_here_is_gone_from_the_next_poll(library):
    _poll(library)

    ArrClient(RADARR_SPEC, "http://radarr.test:7878", "k").unmonitor_movie(9)

    assert "Dune" not in _shown(_poll(library))


def test_the_library_is_read_again_once_the_hold_is_over(library):
    _poll(library)
    library.clock[0] += _library_hold.HOLD_SECONDS

    _poll(library)

    assert library.library_reads["/api/v3/movie"] == 2


def test_what_acts_on_a_download_reads_the_library_live_every_time(library):
    _poll(library)
    _poll(library, steady=False)
    _poll(library, steady=False)

    assert library.library_reads["/api/v3/movie"] == 3
