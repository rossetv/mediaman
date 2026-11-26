"""The setting that lets Google's Cast script into people's browsers, and the pages it reaches."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.db import set_connection
from mediaman.web import SecurityHeadersMiddleware
from mediaman.web.middleware.security_headers import _CAST_SCRIPTS
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
    assert len(_CAST_SCRIPTS.split()) > 1, "one script only: the words must say so"
    assert "One script" not in told
    assert "Scripts from Google (gstatic.com) run" in told
    assert "Chromium" in told and "Chromium" in needs
    assert "internet address" in told
    assert 'aria-describedby="cast-lets"' in html


def test_an_admin_is_told_which_addresses_older_tvs_take(app, authed_client, conn) -> None:
    html = authed_client(app, conn).get("/settings").text
    hint = html.split('id="tv-address-hint"', 1)[1].split("</span>", 1)[0]
    needs = html.split("<b>An older TV:</b>", 1)[1].split("</li>", 1)[0]
    for said in (hint, needs):
        assert "a home address or a .local name with http" in said
        assert "any address with https" in said


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


def _page(conn, path: str, stored: str, username: str | None) -> str:
    set_connection(conn)
    conn.execute("DELETE FROM settings WHERE key = 'google_cast_enabled'")
    insert_settings(conn, google_cast_enabled=stored)
    app = FastAPI()
    app.state.config = SimpleNamespace(secret_key="k" * 64)
    app.add_middleware(SecurityHeadersMiddleware)
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

    @app.get(path)
    def page(request: Request):
        # As the session guard does, inside the middleware that writes the mark.
        if username:
            request.state.username = username
        return templates.TemplateResponse(request, "base.html", {"static_v": "1"})

    return TestClient(app).get(path).text


def _mark(html: str) -> str | None:
    found = re.search(r'<body[^>]*\bdata-cast-account="([^"]*)"', html)
    return found.group(1) if found else None


def test_the_page_says_whose_it_is_without_saying_who(conn) -> None:
    mine = _mark(_page(conn, "/watch", "true", "maren"))
    assert mine
    assert re.fullmatch(r"[0-9a-f]{32}", mine)
    assert _mark(_page(conn, "/downloads", "true", "maren")) == mine
    assert _mark(_page(conn, "/watch", "true", "tobias")) not in (None, "", mine)


@pytest.mark.parametrize(
    ("path", "stored", "username", "mark"),
    [
        ("/watch", "true", None, ""),
        ("/watch", "false", "maren", None),
        ("/settings", "true", "maren", None),
    ],
)
def test_no_page_without_a_cast_or_a_person_is_marked(conn, path, stored, username, mark) -> None:
    assert _mark(_page(conn, path, stored, username)) == mark


def test_the_settings_words_hold_both_states_of_the_switch(app, authed_client, conn) -> None:
    """They follow the press, before anything is saved, so the page needs both."""
    html = authed_client(app, conn).get("/settings").text
    assert 'class="tv-lets-off">What turning it on lets in:' in html
    assert 'class="tv-lets-on">On. What this lets in:' in html


def test_an_admin_is_told_how_far_googles_script_reaches(app, authed_client, conn) -> None:
    html = authed_client(app, conn).get("/settings").text
    told = html.split('id="cast-lets"', 1)[1].split("</ul>", 1)[0]
    for said in (
        "see and do all that the person signed in there can",
        "the address they open mediaman at",
        "the film’s title and a ticket",
        "Turning it off stops no Chromecast",
    ):
        assert said in told
