"""Every route of the real app refuses a request with no session, unless it is meant to answer one."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.db import set_connection
from mediaman.main import create_app
from tests.unit.web.test_viewer_route_enumeration import _ORIGIN, _concrete, _routes

#: Routes that answer without a session: probes, the sign-in page, a signed link or a
#: ticket, the TV's own device calls, and the redirect of the old /watch address.
_OPEN = {
    ("GET", "/healthz"),
    ("GET", "/readyz"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/tv"),
    ("GET", "/watch"),
    ("HEAD", "/watch"),
    ("GET", "/static/{path}"),
    ("GET", "/api/poster/{rating_key}"),
    ("GET", "/api/download/status"),
    ("GET", "/keep/{token}"),
    ("POST", "/keep/{token}"),
    ("GET", "/download/{token}"),
    ("POST", "/download/{token}"),
    ("GET", "/unsubscribe"),
    ("POST", "/unsubscribe"),
    ("POST", "/api/tv/device/code"),
    ("POST", "/api/tv/device/poll"),
    ("POST", "/api/tv/device/ask"),
    ("POST", "/api/tv/device/unpair"),
    ("GET", "/api/tv/device/art/{play_id}"),
    ("GET", "/api/tv/device/avatar/{tv_id}/{user_id}"),
    ("GET", "/api/cast/{ticket}/index.m3u8"),
    ("HEAD", "/api/cast/{ticket}/index.m3u8"),
    ("OPTIONS", "/api/cast/{ticket}/index.m3u8"),
    ("GET", "/api/cast/{ticket}/seg/{name}"),
    ("HEAD", "/api/cast/{ticket}/seg/{name}"),
    ("OPTIONS", "/api/cast/{ticket}/seg/{name}"),
    ("GET", "/api/cast/{ticket}/poster"),
    ("OPTIONS", "/api/cast/{ticket}/poster"),
}

#: These bodies are checked before the session, so each needs one that passes.
_BODIES = {
    ("POST", "/api/tvs/pair"): {"code": "x"},
    ("PATCH", "/api/tvs/{tv_id}"): {"name": "x"},
    ("POST", "/api/tvs/{tv_id}/play"): {"rating_key": "1"},
    ("POST", "/api/tvs/{tv_id}/commands"): {"kind": "pause"},
}


@pytest.fixture
def signed_out(conn):
    set_connection(conn)
    app = create_app()
    return app, TestClient(app, raise_server_exceptions=False)


def _protected(app) -> list[tuple[str, str]]:
    return [route for route in _routes(app) if route not in _OPEN]


def _routes_for_params() -> list[tuple[str, str]]:
    # Collected without a database: the route table does not depend on one.
    return _protected(create_app())


@pytest.mark.parametrize(("method", "template"), _routes_for_params())
def test_a_route_refuses_a_request_with_no_session(signed_out, method, template):
    _app, client = signed_out
    resp = client.request(
        method,
        _concrete(template),
        headers=_ORIGIN,
        json=_BODIES.get((method, template)),
        follow_redirects=False,
    )

    if template.startswith("/api/"):
        # A redirect would hand the page's fetch the login HTML as a 200.
        assert resp.status_code == 401, (resp.status_code, resp.text[:100])
        return
    if resp.status_code == 401:
        return
    assert resp.status_code in (302, 303), (resp.status_code, resp.text[:100])
    assert resp.headers["location"].split("?")[0] == "/login"


def test_the_open_list_names_routes_that_exist_and_leaves_the_api_guarded(signed_out):
    app, _client = signed_out
    assert set(_routes(app)) >= _OPEN
    guarded = set(_protected(app))
    assert {
        ("GET", "/api/kept"),
        ("POST", "/api/media/{media_item_id}/unprotect"),
        ("GET", "/api/show/{show_rating_key}/seasons"),
        ("GET", "/api/library"),
        ("GET", "/api/search"),
        ("GET", "/api/search/discover"),
        ("GET", "/api/plex/libraries"),
        ("GET", "/api/settings/disk-usage"),
        ("DELETE", "/api/users/sessions/{public_id}"),
        ("GET", "/api/watch/thumb/{rating_key}"),
        ("GET", "/api/watch/img/tmdb/{size}/{name}"),
        ("GET", "/api/watch/stream/{stream_id}/preview/{n}"),
    } <= guarded
