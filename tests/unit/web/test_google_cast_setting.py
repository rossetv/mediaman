"""The setting that lets Google's Cast script into people's browsers, and the pages it reaches."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.db import set_connection
from mediaman.web import SecurityHeadersMiddleware
from mediaman.web.routes.settings import router
from tests.helpers.factories import insert_settings

SCRIPT = "/static/js/watch/cast-sender.js"


@pytest.fixture
def app(app_factory, conn):
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    return app_factory(router, conn=conn, state_extras={"templates": templates})


def _csp() -> str:
    bare = FastAPI()
    bare.add_middleware(SecurityHeadersMiddleware)
    return TestClient(bare).get("/watch").headers["Content-Security-Policy"]


def test_turning_chromecast_on_needs_the_password_again(app, authed_client, conn) -> None:
    client = authed_client(app, conn)
    assert client.put("/api/settings", json={"google_cast_enabled": True}).status_code == 403
    assert "gstatic" not in _csp()


@pytest.mark.parametrize("sent", [True, "true"])
def test_what_the_page_saves_opens_the_policy_and_off_closes_it(
    app, authed_client, conn, sent
) -> None:
    client = authed_client(app, conn, with_reauth=True)
    off = client.get("/settings").text
    assert client.put("/api/settings", json={"google_cast_enabled": sent}).json()["ignored"] == []
    on = client.get("/settings").text
    assert 'id="google_cast_enabled" value="false"' in off
    assert 'id="google_cast_enabled" value="true"' in on
    assert "https://www.gstatic.com/cv/js/sender/v1/cast_sender.js" in _csp()
    client.put("/api/settings", json={"google_cast_enabled": False})
    assert "gstatic" not in _csp()


def test_an_admin_is_told_what_the_switch_lets_in_and_what_each_way_needs(
    app, authed_client, conn
) -> None:
    html = authed_client(app, conn).get("/settings").text
    needs = html.split('id="tv-needs"', 1)[1].split("</ul>", 1)[0]
    assert all(word in needs for word in ("Safari", "https", "Chrome", "MEDIAMAN_ALLOWED_HOSTS"))
    told = html.split('id="cast-lets"', 1)[1].split("</ul>", 1)[0]
    assert "script from Google (gstatic.com)" in told
    assert "internet address" in told
    assert 'aria-describedby="cast-lets"' in html


def _scripts(path: str, conn, stored: str) -> str:
    set_connection(conn)
    insert_settings(conn, google_cast_enabled=stored)
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

    @app.get(path)
    def page(request: Request):
        return templates.TemplateResponse(request, "base.html", {"static_v": "1"})

    return TestClient(app).get(path).text


@pytest.mark.parametrize("path", ["/watch", "/watch/show/7", "/downloads"])
def test_the_page_has_the_sender_only_where_it_may_load_googles(conn, path) -> None:
    html = _scripts(path, conn, "true")
    assert html.count(SCRIPT) == 1
    # The bar of what plays takes it as it finds it.
    assert html.index(SCRIPT) < html.index("/static/js/tv-remote.js")


@pytest.mark.parametrize(
    ("path", "stored"), [("/watch", "false"), ("/settings", "true"), ("/account", "true")]
)
def test_no_other_page_has_it_from_the_base(conn, path, stored) -> None:
    assert SCRIPT not in _scripts(path, conn, stored)
