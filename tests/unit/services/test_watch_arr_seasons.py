"""The held Sonarr view: its seasons, and a page that will not wait for a slow Sonarr."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import SafeHTTPError
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

    assert view.show_seasons(77, None, {}).states[1].status == "downloading"
    assert view.show_seasons(None, 5500, {}).states[1].status == "downloading"
    assert view.show_seasons(78, None, {}) is None
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

    assert (early.answers_for("tv"), early.show_seasons(77, None, {})) == (False, None)
    assert later.answers_for("tv")
    assert later.show_seasons(77, None, {}).states[1].status == "downloading"


def test_dropping_the_view_while_sonarr_hangs_piles_up_no_builds(monkeypatch):
    answered = threading.Event()
    builds: list[str] = []

    def get_series():
        builds.append("build")
        answered.wait(5)
        return [SERIES]

    _hold(monkeypatch, get_series, [])

    for _ in range(5):
        catalog.arr_view(None, "k", wait_s=0)
        catalog.invalidate()
    piled = len(builds)
    answered.set()
    for thread in threading.enumerate():
        if thread.name == "watch-arr":
            thread.join(5)

    assert piled == 1
    # What the hung build read is older than the drop, so it is read anew, behind the page again.
    assert catalog.arr_view(None, "k", wait_s=5).answers_for("tv")
    assert len(builds) == 2


def test_odd_answers_of_sonarr_are_skipped_and_the_show_is_known_to_have_some(monkeypatch):
    odd = {**SERIES, "tmdbId": 78, "id": 10, "seasons": [None, *SERIES["seasons"]]}
    sonarr = SimpleNamespace(
        get_series=lambda: [SERIES, odd, {"tmdbId": 79, "seasons": "x"}, {"tmdbId": [1]}],
        get_queue=lambda: [{"series": "x"}, {**QUEUE[0], "episode": 3}, *QUEUE],
    )
    monkeypatch.setattr(_arr.arr_build, "build_sonarr_from_db", lambda *_: sonarr)

    view = catalog.arr_view(None, "k")

    assert view.answers_for("tv")
    assert view.show_seasons(77, None, {}).states[1].status == "downloading"
    assert view.state("tv", 78) == ("queued", 0)
    found = {t: view.show_seasons(t, None, {}) for t in (77, 78, 79)}
    assert {t: (list(f.states), f.whole) for t, f in found.items()} == {
        77: ([1], True),
        78: ([1], False),
        79: ([], False),
    }


class _Down:
    """A Sonarr that gives no answer until it is *up*."""

    def __init__(self, monkeypatch):
        self.up = False
        self.asked = 0
        self.movies = 0
        self.slow: threading.Event | None = None
        self.waited_out = False
        self.radarr_hangs: threading.Event | None = None
        monkeypatch.setattr(_arr.arr_build, "build_sonarr_from_db", lambda *_: self)
        radarr = SimpleNamespace(get_movies=self._get_movies, get_queue=lambda: [])
        monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", lambda *_: radarr)

    def _get_movies(self):
        self.movies += 1
        if self.radarr_hangs is not None:
            self.radarr_hangs.wait(5)
            self.waited_out = True
            raise SafeHTTPError(status_code=0, body_snippet="", url="http://radarr.invalid")
        return [{"tmdbId": 7, "hasFile": False, "monitored": True}]

    def get_series(self):
        self.asked += 1
        if self.slow is not None:
            self.slow.wait(5)
            self.waited_out = True
        if not self.up:
            raise SafeHTTPError(status_code=503, body_snippet="", url="http://sonarr.invalid")
        return [SERIES]

    def get_queue(self):
        return []


def test_a_sonarr_that_gave_no_answer_is_asked_again_seconds_later_and_alone(monkeypatch):
    sonarr, now = _Down(monkeypatch), [1000.0]

    def view(wait_s=None):
        return catalog.arr_view(None, "k", clock=lambda: now[0], wait_s=wait_s)

    assert not view().answers_for("tv")
    now[0] += 4
    sonarr.up = True
    for _ in range(20):
        assert not view(5).answers_for("tv")
    assert sonarr.asked == 1

    now[0] += 2
    assert view(5).answers_for("tv")
    assert view(5).state("movie", 7) == ("queued", 0)
    assert view(5).answers_for("movie")
    assert (sonarr.asked, sonarr.movies) == (2, 1)
    # Radarr's is as old as it was, so all is read anew thirty seconds after that.
    now[0] += 25
    view()
    assert (sonarr.asked, sonarr.movies) == (3, 2)


def test_a_sonarr_that_stays_down_is_left_alone_for_seconds_after_each_try(monkeypatch):
    sonarr, now = _Down(monkeypatch), [1000.0]

    for _ in range(29):
        catalog.arr_view(None, "k", clock=lambda: now[0])
        now[0] += 1

    assert sonarr.asked == 6


def test_a_page_is_not_held_up_by_a_sonarr_that_is_asked_again(monkeypatch):
    monkeypatch.setattr(_arr, "RETRY_WAIT_S", 0.01)
    sonarr, now = _Down(monkeypatch), [1000.0]
    catalog.arr_view(None, "k", clock=lambda: now[0])
    now[0] += 6
    sonarr.slow = threading.Event()

    try:
        for _ in range(3):
            held = catalog.arr_view(None, "k", clock=lambda: now[0])
            assert held.state("movie", 7) == ("queued", 0)
        assert (sonarr.asked, sonarr.waited_out) == (2, False)
    finally:
        sonarr.slow.set()


def _end_builds(*events):
    for event in events:
        event.set()
    for thread in threading.enumerate():
        if thread.name == "watch-arr":
            thread.join(5)


def test_a_sonarr_that_hangs_longer_than_the_view_is_held_holds_up_no_page(monkeypatch):
    monkeypatch.setattr(_arr, "RETRY_WAIT_S", 0.01)
    sonarr, now = _Down(monkeypatch), [1000.0]

    def view():
        return catalog.arr_view(None, "k", clock=lambda: now[0])

    view()
    now[0] += 6
    sonarr.slow = threading.Event()
    try:
        view()
        # Past the thirty seconds, with Sonarr still being asked.
        now[0] += 60
        for _ in range(3):
            assert view().state("movie", 7) == ("queued", 0)
        assert (sonarr.asked, sonarr.waited_out) == (2, False)
    finally:
        _end_builds(sonarr.slow)

    # It gave no answer: it is left alone for as long as after any other.
    now[0] += 4
    view()
    assert sonarr.asked == 2
    # Then all is asked, behind the page.
    sonarr.slow, sonarr.waited_out = threading.Event(), False
    now[0] += 2
    try:
        assert view().state("movie", 7) == ("queued", 0)
        assert sonarr.waited_out is False
    finally:
        _end_builds(sonarr.slow)
    assert (sonarr.asked, sonarr.movies) == (3, 2)


def test_a_radarr_that_hangs_again_holds_up_no_page(monkeypatch):
    monkeypatch.setattr(_arr, "RETRY_WAIT_S", 0.01)
    sonarr, now = _Down(monkeypatch), [1000.0]
    sonarr.up = True
    sonarr.radarr_hangs = threading.Event()
    sonarr.radarr_hangs.set()

    def view():
        return catalog.arr_view(None, "k", clock=lambda: now[0])

    view()
    now[0] += 20
    view()
    assert sonarr.movies == 1
    sonarr.radarr_hangs, sonarr.waited_out = threading.Event(), False
    now[0] += 11
    try:
        for _ in range(3):
            assert view().show_seasons(77, None, {}) is not None
            now[0] += 60
        assert (sonarr.movies, sonarr.waited_out) == (2, False)
    finally:
        _end_builds(sonarr.radarr_hangs)


def test_a_sonarr_that_answers_late_is_not_asked_again_at_once(monkeypatch):
    monkeypatch.setattr(_arr, "RETRY_WAIT_S", 0.01)
    sonarr, now = _Down(monkeypatch), [1000.0]

    def view():
        return catalog.arr_view(None, "k", clock=lambda: now[0])

    view()
    now[0] += 25
    sonarr.up, sonarr.slow = True, threading.Event()
    view()
    now[0] += 20
    _end_builds(sonarr.slow)

    assert view().answers_for("tv")
    assert sonarr.asked == 2
