"""A service that misses answers for a few seconds stands on its last; missing them for longer is
an outage."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.services.arr import fetcher
from mediaman.services.arr.fetcher import fetch_arr_queue, fetch_arr_queue_result
from mediaman.services.infra import SafeHTTPError

_DUNE = {"id": 1, "title": "Dune", "monitored": True, "isAvailable": True}


@pytest.fixture
def radarr(monkeypatch):
    """A Radarr that lists one film still searched for, a Sonarr that is not set up, and a clock
    that moves only when told."""
    fetcher._forget_answers()
    client = MagicMock()
    client.get_queue.return_value = []
    client.get_movies.return_value = [_DUNE]
    monkeypatch.setattr("mediaman.services.arr.build.build_radarr_from_db", lambda *a: client)
    monkeypatch.setattr("mediaman.services.arr.build.build_sonarr_from_db", lambda *a: None)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(fetcher, "time", SimpleNamespace(monotonic=lambda: clock.now))
    client.clock = clock
    yield client
    fetcher._forget_answers()


def _answers(conn, radarr, outcomes):
    """What each fetch gives, as (films listed, services reported), for answers that are "ok",
    "down" (no connection) or "refused" (a 401)."""
    seen = []
    for outcome in outcomes:
        radarr.get_queue.side_effect = {
            "ok": None,
            "down": requests.ConnectionError("down"),
            "refused": SafeHTTPError(401, "", "http://radarr/api/v3/queue"),
        }[outcome]
        radarr.clock.now += 2
        result = fetch_arr_queue_result(conn, "k" * 64)
        seen.append(([c["title"] for c in result.cards], result.unreachable, result.refused))
    return seen


def test_one_failure_gives_the_last_answer_and_reports_nothing(conn, radarr):
    assert _answers(conn, radarr, ["ok", "down"])[1] == (["Dune"], [], [])


def test_failures_more_than_five_seconds_after_the_first_in_a_row_are_reported(conn, radarr):
    seen = _answers(conn, radarr, ["ok", "down", "down", "down", "down"])
    assert [unreachable for _, unreachable, _ in seen] == [[], [], [], [], ["Radarr"]]
    assert seen[4][0] == []


def test_two_pages_that_each_miss_an_answer_in_one_blip_both_keep_the_last(conn, radarr):
    _answers(conn, radarr, ["ok"])
    radarr.get_queue.side_effect = requests.ConnectionError("down")
    first = fetch_arr_queue_result(conn, "k" * 64)
    second = fetch_arr_queue_result(conn, "k" * 64)
    assert [(r.unreachable, [c["title"] for c in r.cards]) for r in (first, second)] == [
        ([], ["Dune"]),
        ([], ["Dune"]),
    ]


def test_a_blip_that_starts_near_a_minute_after_the_answer_stands_in_only_within_the_minute(
    conn, radarr
):
    _answers(conn, radarr, ["ok"])
    radarr.clock.now += 57
    assert _answers(conn, radarr, ["down", "down"]) == [(["Dune"], [], []), ([], ["Radarr"], [])]


def test_failures_between_answers_are_never_reported(conn, radarr):
    seen = _answers(conn, radarr, ["ok", "down", "ok", "down"])
    assert [unreachable for _, unreachable, _ in seen] == [[], [], [], []]
    assert seen[3][0] == ["Dune"]


def test_an_answer_older_than_a_minute_stands_in_for_nothing(conn, radarr):
    _answers(conn, radarr, ["ok"])
    radarr.clock.now += 61
    assert _answers(conn, radarr, ["down"]) == [([], ["Radarr"], [])]


def test_a_refused_key_is_reported_at_once(conn, radarr):
    assert _answers(conn, radarr, ["ok", "refused"])[1] == ([], ["Radarr"], ["Radarr"])


def test_an_answer_given_in_part_is_a_failure_the_last_answer_stands_in_for(conn, radarr):
    _answers(conn, radarr, ["ok"])
    radarr.get_movies.side_effect = requests.ConnectionError("down")
    result = fetch_arr_queue_result(conn, "k" * 64)
    assert ([c["title"] for c in result.cards], result.unreachable) == (["Dune"], [])


def test_the_scheduled_sweep_never_acts_on_a_last_answer(conn, radarr):
    _answers(conn, radarr, ["ok"])
    radarr.get_queue.side_effect = requests.ConnectionError("down")
    assert fetch_arr_queue(conn, "k" * 64) == []
