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
    return TestClient(bare).get("/").headers["Content-Security-Policy"]


def test_turning_chromecast_on_needs_the_password_again(app, authed_client, conn) -> None:
    client = authed_client(app, conn)
    assert client.put("/api/settings", json={"google_cast_enabled": True}).status_code == 403
    assert "gstatic" not in _csp()


@pytest.mark.parametrize("sent", [True, "true"])
def test_what_the_page_saves_opens_the_policy_and_off_closes_it(
    app, authed_client, conn, sent
) -> None:
    client = authed_client(app, conn, with_reauth=True)
    off = client.get("/settings?pane=1").text
    assert client.put("/api/settings", json={"google_cast_enabled": sent}).json()["ignored"] == []
    on = client.get("/settings?pane=1").text
    assert 'id="google_cast_enabled" value="false"' in off
    assert 'id="google_cast_enabled" value="true"' in on
    assert "https://www.gstatic.com/cv/js/sender/v1/cast_sender.js" in _csp()
    client.put("/api/settings", json={"google_cast_enabled": False})
    assert "gstatic" not in _csp()


def _tvs_section(html: str) -> str:
    return html.split('id="setg-tvs"', 1)[1].split('class="setg-block"', 1)[0]


def test_an_admin_is_told_what_the_switch_lets_in_and_what_each_way_needs(
    app, authed_client, conn
) -> None:
    html = authed_client(app, conn).get("/settings?pane=1").text
    needs = html.split('id="tv-needs"', 1)[1].split("</ul>", 1)[0]
    assert all(word in needs for word in ("Safari", "https", "Chrome"))
    told = html.split('id="cast-lets"', 1)[1].split("</ul>", 1)[0]
    assert len(_CAST_SCRIPTS.split()) > 1, "one script only: the words must say so"
    assert "One script" not in told
    assert "Google’s scripts (gstatic.com) run" in told
    assert "Chromium" in told
    assert "viewer’s IP" in told
    switch = html.split('aria-label="Allow Chromecast"', 1)[1].split(">", 1)[0]
    note = switch.split('aria-describedby="', 1)[1].split('"', 1)[0]
    assert "Loads Google’s cast scripts." in html.split(f'id="{note}"', 1)[1].split("</", 1)[0]


def test_the_section_holds_no_older_tv_control_or_words(app, authed_client, conn) -> None:
    section = _tvs_section(authed_client(app, conn).get("/settings?pane=1").text)
    assert 'id="tv-needs"' in section and 'id="cast-lets"' in section
    for gone in (
        "older",
        "btn-add-tv",
        'id="tv-add"',
        "tv_address",
        "tv-address-hint",
        "MEDIAMAN_ALLOWED_HOSTS",
    ):
        assert gone.lower() not in section.lower()


def _pair_at(html: str) -> str:
    return html.split('class="tv-pair-at">', 1)[1].split("<", 1)[0]


def test_the_way_to_pair_comes_before_the_tvs(app, authed_client, conn) -> None:
    section = _tvs_section(authed_client(app, conn).get("/settings?pane=1").text)
    assert section.index("To pair a TV") < section.index('id="tv-list-wrap"')
    assert "then pair from Play on" in section


def test_with_no_base_url_the_tvs_go_to_where_mediaman_was_opened(app, authed_client, conn) -> None:
    html = authed_client(app, conn).get("/settings?pane=1").text
    assert _pair_at(html) == "testserver/tv"


def test_the_tvs_go_to_the_base_url_without_its_name_and_password(app, authed_client, conn) -> None:
    insert_settings(conn, base_url="https://me:secret@watch.example.org/mm/")
    html = authed_client(app, conn).get("/settings?pane=1").text
    assert _pair_at(html) == "watch.example.org/mm/tv"


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


@pytest.mark.parametrize("path", ["/", "/watch/show/7", "/downloads"])
def test_the_page_has_the_sender_only_where_it_may_load_googles(conn, path) -> None:
    html = _scripts(path, conn, "true")
    assert html.count(SCRIPT) == 1
    # The bar of what plays takes it as it finds it.
    assert html.index(SCRIPT) < html.index("/static/js/tv-remote.js")


@pytest.mark.parametrize(
    ("path", "stored"),
    [("/", "false"), ("/watch", "true"), ("/settings", "true"), ("/account", "true")],
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
    mine = _mark(_page(conn, "/", "true", "maren"))
    assert mine
    assert re.fullmatch(r"[0-9a-f]{32}", mine)
    assert _mark(_page(conn, "/downloads", "true", "maren")) == mine
    assert _mark(_page(conn, "/", "true", "tobias")) not in (None, "", mine)


@pytest.mark.parametrize(
    ("path", "stored", "username", "mark"),
    [
        ("/", "true", None, ""),
        ("/", "false", "maren", None),
        ("/settings", "true", "maren", None),
    ],
)
def test_no_page_without_a_cast_or_a_person_is_marked(conn, path, stored, username, mark) -> None:
    assert _mark(_page(conn, path, stored, username)) == mark


def test_what_the_switch_lets_in_is_a_fold_shut_until_asked(app, authed_client, conn) -> None:
    html = authed_client(app, conn).get("/settings?pane=1").text
    fold = (
        html.split('id="cast-more"', 1)[0].rsplit("<details", 1)[1]
        + html.split('id="cast-more"', 1)[1]
    )
    assert " open" not in fold.split(">", 1)[0]
    summary = fold.split("<summary", 1)[1].split("</summary>", 1)[0]
    assert "What this lets in" in summary
    assert fold.index('id="cast-lets"') < fold.index("</details>")


def test_an_admin_is_told_how_far_googles_script_reaches(app, authed_client, conn) -> None:
    html = authed_client(app, conn).get("/settings?pane=1").text
    told = html.split('id="cast-lets"', 1)[1].split("</ul>", 1)[0]
    for said in (
        "(gstatic.com) run as the viewer",
        "Only in Play on",
        "mediaman’s address",
        "a one-title ticket, no sign-in",
        "Turning it off doesn’t stop a cast already playing",
    ):
        assert said in told
