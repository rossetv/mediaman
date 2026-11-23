"""The menu follows the role on the request; /account serves any signed-in user."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from starlette.requests import Request

from mediaman.web.routes.account import router as account_router

_TEMPLATE_DIR = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATE_DIR))

_VIEWER_LINKS = {"/watch", "/search", "/downloads", "/recommended", "/account"}
_ADMIN_ONLY_LINKS = {"/", "/library", "/history", "/settings"}


def _request(role: str | None, path: str = "/search") -> Request:
    state = {} if role is None else {"user_role": role}
    scope = {"type": "http", "path": path, "query_string": b"", "headers": [], "state": state}
    return Request(scope)


def _render(role: str | None, *, path: str = "/search", **ctx: object) -> str:
    template = _TEMPLATES.env.get_template("_nav.html")
    return template.render(request=_request(role, path), **ctx)


def _hrefs(html: str) -> set[str]:
    return set(re.findall(r'href="([^"]+)"', html))


def _desktop_links(html: str) -> list[str]:
    rail = html.split('<nav class="nav-links"')[1].split("</nav>")[0]
    return re.findall(r'href="([^"]+)"', rail)


def _tabbar_links(html: str) -> list[str]:
    bar = html.split('<nav class="nav-tabs"')[1].split("</nav>")[0]
    return re.findall(r'href="([^"]+)"', bar)


def _more_links(html: str) -> list[str]:
    sheet = html.split('id="nav-more-sheet"')[1]
    return re.findall(r'href="([^"]+)"', sheet)


def test_viewer_menu_has_only_what_a_viewer_can_open():
    html = _render("viewer")

    assert _desktop_links(html) == ["/watch", "/search", "/downloads", "/recommended", "/account"]
    assert _hrefs(html) >= _VIEWER_LINKS
    assert not (_ADMIN_ONLY_LINKS - {"/"}) & set(
        _desktop_links(html) + _tabbar_links(html) + _more_links(html)
    )
    assert "Logout" in html


def test_viewer_phone_bar_is_watch_search_downloads_and_more_holds_the_rest():
    html = _render("viewer")

    assert _tabbar_links(html) == ["/watch", "/search", "/downloads"]
    assert _more_links(html) == ["/recommended", "/account"]


@pytest.mark.parametrize("role", ["admin", None])
def test_admin_and_missing_role_get_todays_menu_plus_watch(role):
    html = _render(role)

    assert _desktop_links(html) == [
        "/",
        "/library",
        "/watch",
        "/search",
        "/recommended",
        "/downloads",
        "/history",
        "/settings",
    ]
    assert "/account" not in _hrefs(html)
    assert _tabbar_links(html) == ["/watch", "/library", "/search", "/downloads"]
    assert _more_links(html) == ["/", "/recommended", "/history", "/settings"]


def test_unknown_role_gets_the_viewer_menu():
    assert "/settings" not in _hrefs(_render("guest"))


@pytest.mark.parametrize("path", ["/watch", "/watch/show/abc", "/watch/play/42"])
def test_watch_pages_highlight_watch_without_nav_active(path):
    html = _render("viewer", path=path)

    assert re.search(r'<a href="/watch"\s+class="nav-btn on"', html)
    assert 'href="/watch" aria-label="Watch"' in html
    assert re.search(r'class="nav-tab on" href="/watch"', html)


def test_a_path_that_only_starts_like_watch_highlights_nothing():
    html = _render("admin", path="/watching")

    assert "nav-btn on" not in html


def test_nav_renders_without_a_request():
    html = _TEMPLATES.env.get_template("_nav.html").render(nav_active="library")

    assert "/settings" in _hrefs(html)
    assert re.search(r'href="/library"\s+class="nav-btn on"', html)


class TestAccountPage:
    @pytest.fixture
    def client(self, app_factory, authed_client, conn):
        app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
        return authed_client(app, conn)

    def test_requires_a_session(self, app_factory, conn):
        app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
        resp = TestClient(app).get("/account", follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_shows_who_you_are_and_the_change_password_form(self, client):
        resp = client.get("/account")

        assert resp.status_code == 200
        assert "admin" in resp.text
        assert 'id="account-password-form"' in resp.text
        for field in ("account-old-password", "account-new-password", "account-confirm-password"):
            assert f'id="{field}"' in resp.text
        assert "/static/js/account.js" in resp.text
        assert "<script>" not in resp.text

    def test_a_viewer_sees_the_viewer_menu_with_account_highlighted(
        self, app_factory, authed_client, conn
    ):
        app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})

        @app.middleware("http")
        async def _as_viewer(request, call_next):
            request.state.user_role = "viewer"
            return await call_next(request)

        resp = authed_client(app, conn).get("/account")

        assert "Viewer" in resp.text
        assert re.search(r'href="/account"\s+class="nav-btn on"', resp.text)
        assert 'href="/settings"' not in resp.text
