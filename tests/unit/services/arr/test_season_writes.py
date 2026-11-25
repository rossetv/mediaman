"""A Sonarr series, which is sent whole, is changed one step at a time, in a short time."""

from __future__ import annotations

import copy
import threading
from contextlib import nullcontext, suppress
from itertools import count

import pytest

from mediaman.services.arr import _sonarr_methods
from mediaman.services.arr._sonarr_methods import SeriesChange
from mediaman.services.arr.base import ArrClient, ArrError, SearchUnanswered
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


def _lock(series_id=9):
    key = ("http://sonarr:8989", series_id)
    return _sonarr_methods._series_locks.setdefault(key, threading.Lock())


def test_somebody_waiting_for_a_series_changed_for_too_long_is_told_it_gave_no_answer(
    client, fake_http, fake_response, monkeypatch
):
    monkeypatch.setattr(_sonarr_methods, "_STEP_SECONDS", 0.05)
    held = Held(fake_response, s3=False)
    fake_http.handler(held)

    with _lock(), client.within(40), pytest.raises(SafeHTTPError) as told:
        client.remonitor_season(9, 3)

    assert told.value.status_code == 0
    assert held.calls == []


def test_a_series_being_changed_does_not_hold_up_another(client, fake_http, fake_response):
    def arr(method, url, **_kwargs):
        series = {"id": 8, "seasons": [{"seasonNumber": 3, "monitored": True}]}
        return fake_response(json_data=series if "series" in url else [] if method == "GET" else {})

    fake_http.handler(arr)
    with _lock(), client.within(1):
        client.remonitor_season(8, 3)


def _run(client, change):
    if change == "within":
        with client.within(40):
            client.remonitor_season(9, 3, max_retries=1)
    else:
        client.remonitor_season(9, 3)


@pytest.mark.parametrize("change", ["within", "unhurried"])
def test_only_a_change_somebody_waits_for_is_hurried(
    client, fake_http, fake_response, monkeypatch, change
):
    clock = count(0.0, 4.0)
    monkeypatch.setattr(_sonarr_methods, "monotonic", lambda: next(clock))
    timeouts = []
    held = Held(fake_response, s3=False)

    def arr(method, url, **kwargs):
        timeouts.append(kwargs["timeout"])
        return held(method, url, **kwargs)

    fake_http.handler(arr)
    with suppress(SafeHTTPError, ArrError):
        _run(client, change)

    reads = [read for _connect, read in timeouts]
    assert (max(reads) <= _sonarr_methods._STEP_SECONDS) is (change == "within")
    # The list of commands is read in a short time of its own.
    assert change == "within" or sorted(reads) == [5.0, 30.0, 30.0, 30.0]


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

    with client.within(40), pytest.raises(SafeHTTPError):
        client.remonitor_season(9, 3)

    assert sum(read for _connect, read in timeouts) <= 2 * _sonarr_methods._STEP_SECONDS


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
    with client.within(40):
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


@pytest.mark.parametrize("stats", [{"episodeCount": 2}, {"nextAiring": "2099-01-01T00:00:00Z"}])
def test_a_season_with_episodes_monitored_one_by_one_is_not_put_back_over_them(
    client, fake_http, fake_response, stats
):
    held = Held(fake_response, s3=False)
    held.series["seasons"][0]["statistics"] = stats
    held.failing_searches = 1
    fake_http.handler(held)

    with pytest.raises(SearchUnanswered):
        client.remonitor_season(9, 3)

    assert held.seasons == {3: True}


def test_a_change_tried_again_is_not_judged_by_what_its_first_try_did(
    client, fake_http, fake_response
):
    held = Held(fake_response, s3=False)
    judged = []

    def arr(method, url, **kwargs):
        response = held(method, url, **kwargs)
        return fake_response(status=502, text="no") if method == "PUT" else response

    fake_http.handler(arr)
    client.remonitor_season(9, 3, guard=lambda series: judged.append(copy.deepcopy(series)))

    assert [s["seasons"][0]["monitored"] for s in judged] == [False]
    assert held.seasons == {3: True}


def test_what_a_season_deleted_meanwhile_changed_is_taken_back_with_the_show(
    client, fake_http, fake_response
):
    held = Held(fake_response, monitored=False, s3=False, s4=True)
    fake_http.handler(held)
    change = SeriesChange()
    with client.within(40):
        client.remonitor_season(9, 3, change=change)
        turned_on = held.series["monitored"]
        # Whatever time the rest has used up.
        with client.within(-1):
            client.take_back_season(9, 3, change)

    assert turned_on is True
    assert held.seasons == {3: False, 4: True}
    assert (held.series["monitored"], change.left_on) == (False, False)


@pytest.mark.parametrize("busy", [True, False])
def test_a_show_that_cannot_be_taken_back_is_known_to_be_left_on(
    client, fake_http, fake_response, monkeypatch, busy
):
    monkeypatch.setattr(_sonarr_methods, "_UNDO_SECONDS", 0.05)
    held = Held(fake_response, monitored=False, s3=False)
    fake_http.handler(held)
    change = SeriesChange()
    client.remonitor_season(9, 3, change=change)
    if not busy:
        fake_http.handler(lambda *_a, **_k: fake_response(status=500, text="no"))
    with _lock() if busy else nullcontext():
        client.take_back_season(9, 3, change)

    assert change.left_on is True


def _put_lost(held, fake_response, *, lands, then=None):
    """*held* while the answer to its first change is lost, which it took only if *lands*; *then*
    is what its calls answer after."""
    lost = []

    def arr(method, url, **kwargs):
        if lost and then is not None:
            return fake_response(status=then, text="no")
        if method == "PUT" and not lost:
            lost.append(url)
            if lands:
                held(method, url, **kwargs)
            raise SafeHTTPError(0, "timeout", url)
        return held(method, url, **kwargs)

    return arr


def test_whether_a_show_stays_on_is_unknown_when_the_change_and_the_put_back_got_no_answer(
    client, fake_http, fake_response
):
    held = Held(fake_response, monitored=False, s3=False)
    fake_http.handler(_put_lost(held, fake_response, lands=False, then=500))
    change = SeriesChange()

    with pytest.raises(SafeHTTPError):
        client.remonitor_season(9, 3, max_retries=1, change=change)

    assert change.left_on is None


def test_a_show_read_as_on_that_could_not_be_put_back_is_known_to_be_left_on(
    client, fake_http, fake_response
):
    held = Held(fake_response, monitored=False, s3=False)

    def arr(method, url, **kwargs):
        if method == "PUT":
            held(method, url, **kwargs)
            raise SafeHTTPError(0, "timeout", url)
        return held(method, url, **kwargs)

    # The put-back is refused, so the show stays as the first change left it.
    puts = count(1)
    fake_http.handler(
        lambda method, url, **kwargs: (
            fake_response(status=500, text="no")
            if method == "PUT" and next(puts) > 1
            else arr(method, url, **kwargs)
        )
    )
    change = SeriesChange()

    with pytest.raises(SafeHTTPError):
        client.remonitor_season(9, 3, max_retries=1, change=change)

    assert (held.series["monitored"], change.left_on) == (True, True)


def test_a_show_a_change_never_reached_is_not_left_on_for_a_season_asked_for_since(
    client, fake_http, fake_response
):
    held = Held(fake_response, monitored=False, s3=False, s4=False)
    lost = _put_lost(held, fake_response, lands=False)

    def arr(method, url, **kwargs):
        try:
            return lost(method, url, **kwargs)
        except SafeHTTPError:
            held.series["seasons"][1]["monitored"] = True
            raise

    fake_http.handler(arr)
    change = SeriesChange()

    with pytest.raises(SafeHTTPError):
        client.remonitor_season(9, 3, max_retries=1, change=change)

    assert (held.series["monitored"], change.left_on) == (False, False)


@pytest.mark.parametrize("lands", [True, False])
def test_a_season_of_lone_episodes_is_on_its_way_when_only_the_answer_was_lost(
    client, fake_http, fake_response, lands
):
    held = Held(fake_response, monitored=False, s3=False)
    held.series["seasons"][0]["statistics"] = {"episodeCount": 2}
    fake_http.handler(_put_lost(held, fake_response, lands=lands))
    change = SeriesChange()

    with pytest.raises(SearchUnanswered if lands else SafeHTTPError):
        client.remonitor_season(9, 3, max_retries=1, change=change)

    assert (held.seasons, held.series["monitored"]) == ({3: lands}, lands)
    assert change.left_on is False
