"""A row id in the address past the largest SQLite takes is refused as malformed, never a 500."""

from __future__ import annotations

import pytest

from mediaman.web.routes import plex_link, subscribers
from mediaman.web.routes.search import assist
from mediaman.web.routes.users import (
    _REAUTH_LIMITER,
    _USER_CREATE_LIMITER,
    _USER_MGMT_LIMITER,
)
from mediaman.web.routes.users import router as users_router

_LARGEST = 2**63 - 1

_ROUTES = [
    ("delete", "/api/users/{}", None),
    ("patch", "/api/users/{}/role", {"role": "viewer"}),
    ("patch", "/api/users/{}/email", {"email": "a@example.com"}),
    ("post", "/api/users/{}/unlock", None),
    ("delete", "/api/users/{}/plex-link", None),
    ("delete", "/api/subscribers/{}", None),
    ("get", "/api/search/assist/runs/{}", None),
    ("delete", "/api/search/assist/runs/{}", None),
]


@pytest.fixture(autouse=True)
def _reset_limiters():
    limiters = (_USER_MGMT_LIMITER, _USER_CREATE_LIMITER, _REAUTH_LIMITER)
    for lim in limiters:
        lim.reset()
    yield
    for lim in limiters:
        lim.reset()


@pytest.fixture
def client(app_factory, authed_client, conn):
    app = app_factory(users_router, plex_link.router, subscribers.router, assist.router, conn=conn)
    return authed_client(app, conn, with_reauth=True)


def _send(client, method, url, body, number):
    kwargs = {"json": body} if body is not None else {}
    return client.request(method.upper(), url.format(number), **kwargs)


@pytest.mark.parametrize(("method", "url", "body"), _ROUTES)
def test_an_id_past_the_largest_sqlite_takes_is_refused_as_malformed(client, method, url, body):
    assert _send(client, method, url, body, _LARGEST + 1).status_code == 422


# The poll route is left out: with no AI set up it answers that it is unavailable first.
@pytest.mark.parametrize(
    ("method", "url", "body", "status"),
    # Deleting no such user keeps its own answer, shared with deleting yourself.
    [(*_ROUTES[0], 400), *((*route, 404) for route in _ROUTES[1:6]), (*_ROUTES[7], 404)],
)
def test_the_largest_id_sqlite_takes_is_still_answered_as_no_such_row(
    client, method, url, body, status
):
    assert _send(client, method, url, body, _LARGEST).status_code == status
