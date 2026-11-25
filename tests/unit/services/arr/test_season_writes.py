"""A Sonarr series, which is sent whole, is changed one step at a time, in a short time."""

from __future__ import annotations

import copy
import threading
import time
from itertools import count

import pytest

from mediaman.services.arr import _sonarr_methods
from mediaman.services.arr.base import ArrClient, ArrError
from mediaman.services.arr.spec import SONARR_SPEC
from mediaman.services.infra import SafeHTTPError

SERIES = "/api/v3/series/9"


@pytest.fixture
def client():
    return ArrClient(SONARR_SPEC, "http://sonarr:8989", "key")


class Held:
    """A Sonarr holding one series, with something that happens meanwhile."""

    def __init__(self, fake_response, *, monitored=True, **seasons):
        self.series = {
            "id": 9,
            "monitored": monitored,
            "seasons": [{"seasonNumber": int(n[1:]), "monitored": m} for n, m in seasons.items()],
        }
        self._response = fake_response
        self.failing_searches = 0
        self.on: dict[str, object] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url))
        answer = copy.deepcopy(self.series)
        what = "search" if method == "POST" else method if url.endswith(SERIES) else "other"
        fails = what == "search" and self.failing_searches > 0
        self.failing_searches -= fails
        if (meanwhile := self.on.pop(what, None)) is not None:
            meanwhile()
        if fails:
            return self._response(status=400, text="no")
        if what == "PUT":
            self.series = copy.deepcopy(kwargs["json"])
        return self._response(json_data=answer if what == "GET" else [] if method == "GET" else {})

    @property
    def seasons(self):
        return {s["seasonNumber"]: s["monitored"] for s in self.series["seasons"] if s}


def _beside(threads, action, *args):
    """Run *action* while a call of another is held up, for as long as it takes to get in."""

    def run():
        threads.append(threading.Thread(target=action, args=args))
        threads[0].start()
        threads[0].join(0.2)

    return run


@pytest.mark.parametrize("second", ["remonitor_season", "unmonitor_season"])
def test_two_writes_of_one_series_do_not_undo_each_other(client, fake_http, fake_response, second):
    held = Held(fake_response, s3=False, s4=second == "unmonitor_season")
    threads: list[threading.Thread] = []
    held.on["GET"] = _beside(threads, getattr(client, second), 9, 4)
    fake_http.handler(held)

    client.remonitor_season(9, 3)
    threads[0].join(5)

    assert held.seasons == {3: True, 4: second == "remonitor_season"}


def test_a_search_that_fails_puts_back_its_own_season_and_not_one_asked_for_meanwhile(
    client, fake_http, fake_response
):
    held = Held(fake_response, monitored=False, s3=False, s4=False)
    held.failing_searches = 1
    threads: list[threading.Thread] = []
    held.on["search"] = _beside(threads, client.remonitor_season, 9, 4)
    fake_http.handler(held)

    with pytest.raises(SafeHTTPError):
        client.remonitor_season(9, 3)
    threads[0].join(5)

    assert held.seasons == {3: False, 4: True}
    assert held.series["monitored"] is True


def test_the_show_stays_monitored_when_sonarr_was_asked_for_another_season_since(
    client, fake_http, fake_response
):
    held = Held(fake_response, monitored=False, s3=False, s4=False)
    held.failing_searches = 1

    def in_sonarr_itself():
        held.series["seasons"][1]["monitored"] = True

    held.on["search"] = in_sonarr_itself
    fake_http.handler(held)

    with pytest.raises(SafeHTTPError):
        client.remonitor_season(9, 3)

    assert held.seasons == {3: False, 4: True}
    assert held.series["monitored"] is True


@pytest.mark.parametrize("was_on", [True, False])
def test_a_failed_season_is_put_back_with_the_show_it_monitored(
    client, fake_http, fake_response, was_on
):
    held = Held(fake_response, monitored=was_on, s3=False, s4=True)
    held.failing_searches = 1
    fake_http.handler(held)

    with pytest.raises(SafeHTTPError):
        client.remonitor_season(9, 3)

    assert held.seasons == {3: False, 4: True}
    assert held.series["monitored"] is was_on


def test_a_guard_that_refuses_the_series_as_read_stops_the_change(client, fake_http, fake_response):
    held = Held(fake_response, s3=False)
    fake_http.handler(held)

    def guard(series):
        raise ArrError("refused")

    with pytest.raises(ArrError, match="refused"):
        client.remonitor_season(9, 3, guard=guard)

    assert [method for method, _url in held.calls] == ["GET"]


def test_a_series_changed_for_too_long_holds_up_nobody_and_no_other_series(
    client, fake_http, fake_response, monkeypatch
):
    monkeypatch.setattr(_sonarr_methods, "_STEP_SECONDS", 0.05)
    lock = _sonarr_methods._series_locks.setdefault(("http://sonarr:8989", 9), threading.Lock())
    held = Held(fake_response, s3=False)
    fake_http.handler(held)
    with lock:
        before = time.monotonic()
        with pytest.raises(ArrError):
            client.remonitor_season(9, 3)
        waited = time.monotonic() - before

        fake_http.handler(
            lambda method, url, **_kw: fake_response(
                json_data={"id": 8, "seasons": [{"seasonNumber": 3, "monitored": True}]}
                if "series" in url
                else []
                if method == "GET"
                else {}
            )
        )
        client.remonitor_season(8, 3)

    assert waited < 2
    assert held.calls == []


def test_a_hung_sonarr_holds_a_series_for_a_short_time_in_all(
    client, fake_http, fake_response, monkeypatch
):
    clock = count(0.0, 4.0)
    monkeypatch.setattr(_sonarr_methods, "monotonic", lambda: next(clock))
    monkeypatch.setattr(_sonarr_methods, "sleep", lambda _s: None)
    timeouts = []

    def arr(method, url, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise SafeHTTPError(0, "timeout", url)

    fake_http.handler(arr)

    with pytest.raises((SafeHTTPError, ArrError)):
        client.remonitor_season(9, 3)

    assert sum(read for _connect, read in timeouts) <= 2 * _sonarr_methods._STEP_SECONDS
    assert all(read <= _sonarr_methods._STEP_SECONDS for _connect, read in timeouts)


def test_a_read_that_failed_for_a_moment_is_tried_again(
    client, fake_http, fake_response, monkeypatch
):
    monkeypatch.setattr(_sonarr_methods, "sleep", lambda _s: None)
    held = Held(fake_response, s3=True)
    answers = iter([503])

    def arr(method, url, **kwargs):
        status = next(answers, None)
        return fake_response(status=status, text="no") if status else held(method, url, **kwargs)

    fake_http.handler(arr)
    client.unmonitor_season(9, 3)

    assert held.seasons == {3: False}


def test_a_season_of_no_shape_beside_it_does_not_stop_a_change_and_is_sent_back_as_it_was(
    client, fake_http, fake_response
):
    held = Held(fake_response, s3=False)
    held.series["seasons"].insert(0, None)
    fake_http.handler(held)

    client.remonitor_season(9, 3)

    assert held.series["seasons"][0] is None
    assert held.seasons == {3: True}
