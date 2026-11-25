"""Writes of one Sonarr series, which is sent whole, come one at a time."""

from __future__ import annotations

import copy
import threading

import pytest

from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.spec import SONARR_SPEC


@pytest.mark.parametrize("second", ["remonitor_season", "unmonitor_season"])
def test_two_writes_of_one_series_do_not_undo_each_other(fake_http, fake_response, second):
    client = ArrClient(SONARR_SPEC, "http://sonarr:8989", "key")
    held = {
        "id": 9,
        "monitored": True,
        "seasons": [
            {"seasonNumber": 3, "monitored": False},
            {"seasonNumber": 4, "monitored": second == "unmonitor_season"},
        ],
    }
    others: list[threading.Thread] = []

    def arr(method, url, **kwargs):
        if method == "PUT":
            held.update(copy.deepcopy(kwargs["json"]))
        if method != "GET" or "command" in url or "episode" in url:
            return fake_response(json_data=[] if method == "GET" else {})
        answer = copy.deepcopy(held)
        if not others:
            # The other write comes in between this read and its write.
            others.append(threading.Thread(target=getattr(client, second), args=(9, 4)))
            others[0].start()
            others[0].join(0.2)
        return fake_response(json_data=answer)

    fake_http.handler(arr)
    client.remonitor_season(9, 3)
    others[0].join(5)

    assert {s["seasonNumber"]: s["monitored"] for s in held["seasons"]} == {
        3: True,
        4: second == "remonitor_season",
    }
