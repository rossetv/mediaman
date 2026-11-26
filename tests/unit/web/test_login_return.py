"""After signing in, a person is back at the page they asked for."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse

import mediaman.web
from mediaman.web.auth.middleware import resolve_page_session
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.return_path import safe_return_path
from mediaman.web.routes.auth import _limiter
from mediaman.web.routes.auth import router as auth_router
from mediaman.web.routes.kept import router as kept_router

HARNESS = Path(__file__).parent / "js" / "api_login_harness.js"
JUMP_HARNESS = Path(__file__).parent / "js" / "login_jump_harness.js"
_PASSWORD = "correct horse battery 1"
_CREDENTIALS = {"username": "admin", "password": _PASSWORD}

_pages = APIRouter()


@_pages.api_route("/{path:path}", methods=["GET", "POST"])
def _page(request: Request):
    resolved = resolve_page_session(request)
    return resolved if isinstance(resolved, RedirectResponse) else PlainTextResponse("the page")


@pytest.fixture
def client(app_factory, conn):
    _limiter.reset()
    templates = Jinja2Templates(directory=str(Path(mediaman.web.__file__).parent / "templates"))
    app = app_factory(
        auth_router, kept_router, _pages, conn=conn, state_extras={"templates": templates}
    )
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def admin(conn):
    create_user(conn, "admin", _PASSWORD, enforce_policy=False)


@pytest.mark.parametrize(
    "page",
    [
        "/history",
        "/?view=list&q=a%20%26%20b&page=3",
        "/watch/show/Caf%C3%A9%20%3F?season=2",
        "/?q=%2F%2Fexample.com",
        "/kept",
        "/kept/page",
        "/protected?from=mail",
    ],
)
def test_signing_in_again_leads_back_to_the_page_asked_for(client, admin, page):
    to_login = client.get(page).headers["location"]

    assert urlsplit(to_login).path == "/login"
    resp = client.post(to_login, data=_CREDENTIALS)
    assert (resp.status_code, resp.headers["location"]) == (302, page)


def test_a_wrong_password_on_the_way_does_not_lose_the_page(client, admin):
    to_login = client.get("/history?page=2").headers["location"]

    form = client.post(to_login, data={"username": "admin", "password": "wrong"})
    # With no action the form posts to the address it was shown at.
    assert "action=" not in form.text.split("<form")[1].split(">")[0]

    resp = client.post(to_login, data=_CREDENTIALS)
    assert resp.headers["location"] == "/history?page=2"


@pytest.mark.parametrize("page", ["/", "/watch/list?q=x", "/api/history"])
def test_an_address_with_no_place_to_keep_leads_to_plain_sign_in(client, page):
    assert client.get(page).headers["location"] == "/login"


def test_a_post_is_not_a_place_to_return_to(client):
    assert client.post("/settings").headers["location"] == "/login"


@pytest.mark.parametrize(
    "target",
    [
        "history",
        "https://example.com/",
        "//example.com",
        "///example.com",
        "/\\example.com",
        "\\\\example.com",
        "/watch\\..\\x",
        "/%5Cexample.com",
        "/%2Fexample.com",
        "/watch//x",
        "/\t/example.com",
        "/\n/example.com",
        " /watch",
        "/watch\r\nSet-Cookie: a=b",
        "/watch%0d%0aX:y",
        "/wat\x00ch",
        "/watch\u2028",
        "javascript:alert(1)",
        "/api/history",
        "/api",
        "/%61pi/history",
        "/api?x=1",
        "/watch/../api/history",
        "/watch/%2e%2E/api/history",
        "/./api/history",
        "/api/auth/logout",
        "/login",
        "/login?next=/login",
        "/static/js/nav.js",
        "/watch/list",
        "/history#x",
        "/" + "a" * 3000,
    ],
)
def test_an_address_outside_the_pages_of_this_app_is_no_place_to_return_to(target):
    assert safe_return_path(target) is None


@pytest.mark.parametrize("target", ["//example.com", "/api/auth/logout"])
def test_signing_in_never_follows_an_address_outside_the_pages_of_this_app(client, admin, target):
    resp = client.post("/login?next=" + quote(target, safe=""), data=_CREDENTIALS)

    assert (resp.status_code, resp.headers["location"]) == (302, "/")


def _run(*args: str, **location: str) -> str:
    harness = JUMP_HARNESS if args else HARNESS
    result = subprocess.run(
        ["node", str(harness), *args, json.dumps(location)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["href"]


def test_a_session_that_ends_on_an_open_page_keeps_its_address_and_hash():
    href = urlsplit(_run(pathname="/", search="?view=list&q=a%26b", hash="#title=movie-1"))

    assert href.path == "/login"
    assert parse_qs(href.query) == {"next": ["/?view=list&q=a%26b"]}
    assert href.fragment == "title=movie-1"


def test_a_session_that_ends_on_the_landing_page_needs_no_address():
    assert _run(pathname="/", search="", hash="") == "/login"


@pytest.mark.parametrize(
    ("part", "pathname"),
    [("poll", "/downloads"), ("browse", "/"), ("browse", "/watch/show/Caf%C3%A9")],
)
def test_a_page_that_is_refused_while_open_keeps_its_address_and_hash(part, pathname):
    href = urlsplit(_run(part, pathname=pathname, search="?a=1%262", hash="#title=movie-1"))

    assert href.path == "/login"
    assert parse_qs(href.query) == {"next": [pathname + "?a=1%262"]}
    assert href.fragment == "title=movie-1"


def test_the_rows_of_the_landing_page_need_no_address():
    assert _run("browse", pathname="/", search="", hash="") == "/login"
