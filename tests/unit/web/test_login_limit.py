"""The sign-in limit counts failures only and says how long to wait."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman.web
from mediaman.services.rate_limit import limiters
from mediaman.web.auth.password_hash import create_user
from mediaman.web.routes.auth import _limiter
from mediaman.web.routes.auth import router as auth_router

_PASSWORD = "correct horse battery 1"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(limiters, "time", fake)
    return fake


@pytest.fixture
def client(app_factory, conn, clock):
    _limiter.reset()
    templates = Jinja2Templates(directory=str(Path(mediaman.web.__file__).parent / "templates"))
    app = app_factory(auth_router, conn=conn, state_extras={"templates": templates})
    create_user(conn, "admin", _PASSWORD, enforce_policy=False)
    create_user(conn, "other", _PASSWORD, enforce_policy=False)
    return TestClient(app, follow_redirects=False)


def _sign_in(client, username="admin", password=_PASSWORD):
    return client.post("/login", data={"username": username, "password": password})


def test_many_good_sign_ins_from_one_network_are_not_limited(client):
    assert [_sign_in(client).status_code for _ in range(7)] == [302] * 7


def test_good_sign_ins_give_no_failures_back(client):
    names = iter(("a", "b", "c", "d", "e", "f"))
    statuses = []
    for _ in range(5):
        statuses.append(_sign_in(client, next(names), "wrong").status_code)
        if len(statuses) < 5:
            assert _sign_in(client, "other").status_code == 302

    assert statuses == [200] * 5
    assert _sign_in(client, next(names), "wrong").status_code == 429
    assert _sign_in(client).status_code == 429


def test_a_good_sign_in_leaves_the_times_of_the_failures_as_they_were(client, clock):
    _sign_in(client, "a", "wrong")
    clock.now += 100
    _sign_in(client, "other")
    for name in ("b", "c", "d", "e"):
        _sign_in(client, name, "wrong")

    assert _sign_in(client).headers["retry-after"] == "200"


def test_an_attempt_that_is_refused_does_not_lengthen_the_wait(client, clock):
    for name in ("a", "b", "c", "d", "e"):
        _sign_in(client, name, "wrong")
    clock.now += 299
    assert _sign_in(client).status_code == 429
    clock.now += 1

    assert _sign_in(client).status_code == 302


@pytest.mark.parametrize(
    ("waited", "seconds", "words"),
    [(0, "300", "in 5 minutes"), (239.5, "61", "in 2 minutes"), (241, "59", "in 1 minute.")],
)
def test_a_limited_sign_in_says_how_long_to_wait(client, clock, waited, seconds, words):
    for name in ("a", "b", "c", "d", "e"):
        _sign_in(client, name, "wrong")
    clock.now += waited

    resp = _sign_in(client)

    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["retry-after"] == seconds
    assert words in resp.text


def test_a_sign_in_under_way_holds_its_place_in_the_limit():
    limiter = limiters.RateLimiter(max_attempts=2, window_seconds=60)

    first = limiter.reserve("192.0.2.1")
    assert limiter.reserve("192.0.2.2") is not None
    assert limiter.reserve("192.0.2.3") is None

    limiter.release("192.0.2.1", first)
    assert limiter.reserve("192.0.2.3") is not None
    assert limiter.reserve("192.0.2.3") is None
