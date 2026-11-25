"""The held Sonarr view: its seasons, and a page that will not wait for a slow Sonarr."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from mediaman.services import watch_catalog as catalog
from mediaman.services.watch_catalog import _arr

SERIES = {
    "id": 9,
    "tmdbId": 77,
    "tvdbId": 5500,
    "monitored": True,
    "seasons": [
        {"seasonNumber": 1, "monitored": True, "statistics": {"totalEpisodeCount": 8}},
    ],
}
QUEUE = [
    {
        "series": {"id": 9, "tmdbId": 77},
        "episode": {"seasonNumber": 1},
        "status": "downloading",
        "size": 10,
        "sizeleft": 5,
    }
]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    catalog.invalidate()
    monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", lambda *_: None)
    yield
    catalog.invalidate()


def _hold(monkeypatch, get_series, reads):
    def get_queue():
        reads.append("queue")
        return QUEUE

    sonarr = SimpleNamespace(get_series=get_series, get_queue=get_queue)
    monkeypatch.setattr(_arr.arr_build, "build_sonarr_from_db", lambda *_: sonarr)


def test_the_seasons_come_from_the_one_read_of_the_queue(monkeypatch):
    reads: list[str] = []
    _hold(monkeypatch, lambda: [SERIES], reads)

    view = catalog.arr_view(None, "k")

    assert view.season_states(77, None)[1].status == "downloading"
    assert view.season_states(None, 5500)[1].status == "downloading"
    assert view.season_states(78, None) is None
    assert reads == ["queue"]


def test_a_page_that_will_not_wait_is_answered_empty_and_the_next_one_has_the_view(monkeypatch):
    answered = threading.Event()

    def get_series():
        answered.wait(5)
        return [SERIES]

    _hold(monkeypatch, get_series, [])

    early = catalog.arr_view(None, "k", wait_s=0.01)
    answered.set()
    later = catalog.arr_view(None, "k", wait_s=5)

    assert (early.answers_for("tv"), early.season_states(77, None)) == (False, None)
    assert later.answers_for("tv")
    assert later.season_states(77, None)[1].status == "downloading"


def test_dropping_the_view_while_sonarr_hangs_piles_up_no_builds(monkeypatch):
    answered = threading.Event()
    builds: list[str] = []
    done = threading.Semaphore(0)

    def get_series():
        builds.append("build")
        answered.wait(5)
        done.release()
        return [SERIES]

    _hold(monkeypatch, get_series, [])

    for _ in range(5):
        catalog.arr_view(None, "k", wait_s=0)
        catalog.invalidate()
    piled = len(builds)
    answered.set()
    done.acquire(timeout=5)

    assert piled == 1
    # What the hung build read is older than the drop, so it is read anew.
    assert catalog.arr_view(None, "k").answers_for("tv")
    assert len(builds) == 2
