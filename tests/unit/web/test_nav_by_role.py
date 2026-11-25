"""The nav and the profile menu follow the role on the request; /account serves any signed-in user."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader
from starlette.requests import Request

from mediaman.web.auth.password_hash import set_user_email
from mediaman.web.routes.account import router as account_router
from tests.helpers.factories import insert_admin_user

_TEMPLATE_DIR = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATE_DIR))

_ADMIN_ONLY_LINKS = {"/settings"}
_RETIRED_LINKS = {"/search", "/library", "/recommended"}


def _request(role: str | None, path: str = "/watch", username: str | None = "robin") -> Request:
    state = {} if role is None else {"user_role": role}
    if username is not None:
        state["username"] = username
    scope = {"type": "http", "path": path, "query_string": b"", "headers": [], "state": state}
    return Request(scope)


def _render(
    role: str | None, *, path: str = "/watch", username: str | None = "robin", **ctx: object
) -> str:
    template = _TEMPLATES.env.get_template("_nav.html")
    return template.render(request=_request(role, path, username), **ctx)


def _links(html: str) -> list[str]:
    return re.findall(r'<a\b[^>]*\bhref="([^"]+)"', html)


def _menu(html: str) -> str:
    return html.split('id="profile-menu"')[1]


def _menu_items(html: str) -> list[tuple[str, str]]:
    """Each menu item's link (or its form's action) and label, in order."""
    items = re.findall(
        r'(?:href|action)="([^"]+)"[^>]*>(?:\s*<button[^>]*>)?\s*(?:<svg.*?</svg>)?\s*([^<\s][^<]*)',
        _menu(html),
        re.S,
    )
    return [(target, label.strip()) for target, label in items]


def _buttons(html: str) -> list[str]:
    return re.findall(r'<button[^>]*class="profile-btn[^>]*>', html)


def test_a_viewer_gets_the_way_back_to_watch_and_a_menu_of_what_they_may_open():
    html = _render("viewer")

    assert set(_links(_bar_of(html).split("<noscript>")[0])) == {"/watch"}
    assert _menu_items(html) == [
        ("/account", "Profile"),
        ("/downloads", "Downloads"),
        ("/history", "History"),
        ("/api/auth/logout", "Log out"),
    ]
    assert not _ADMIN_ONLY_LINKS & set(_links(html))


@pytest.mark.parametrize("role", ["admin", None])
def test_an_admin_and_a_missing_role_find_settings_too_in_the_menu_and_nothing_more_in_the_bar(
    role,
):
    html = _render(role)

    assert set(_links(_bar_of(html).split("<noscript>")[0])) == {"/watch"}
    assert _menu_items(html) == [
        ("/account", "Profile"),
        ("/downloads", "Downloads"),
        ("/history", "History"),
        ("/settings", "Settings"),
        ("/api/auth/logout", "Log out"),
    ]


def _bar_of(html: str) -> str:
    return html.split('<header class="nav-glass')[1].split("</header>")[0]


def _watch_bar(role: str, **ctx: object) -> str:
    template = _TEMPLATES.env.get_template("_watch_bar.html")
    return template.render(request=_request(role), **ctx)


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_no_page_has_a_menu_of_links_in_its_bar_or_at_its_foot(role):
    for html in (_render(role), _watch_bar(role, is_admin=role == "admin", view="rows", q="")):
        assert "<nav" not in html
        assert re.search(r'<div class="nav-foot" aria-hidden="true"></div>', html)


def test_without_scripts_downloads_is_still_a_link_in_every_bar():
    for html in (_render("viewer"), _watch_bar("viewer", is_admin=False, view="rows", q="")):
        assert re.search(r'<noscript><a [^>]*href="/downloads">', _bar_of(html))


def test_on_watch_one_bar_holds_the_search_and_the_storage_in_reading_order():
    html = _watch_bar("admin", is_admin=True, view="rows", q="")

    order = ('class="brand"', 'id="watch-search"', "space-meter", "profile-btn")
    places = [_bar_of(html).index(part) for part in order]
    assert places == sorted(places)
    assert len(_buttons(html)) == 1
    assert "nav-back" not in html


def test_a_viewer_s_bar_on_watch_has_the_search_and_no_storage():
    bar = _bar_of(_watch_bar("viewer", is_admin=False, view="rows", q=""))

    assert 'id="watch-search"' in bar
    assert "space-meter" not in bar


@pytest.mark.parametrize(
    ("role", "view"), [("admin", "rows"), ("admin", "list"), ("viewer", "rows")]
)
def test_nobody_is_offered_two_views_of_watch(role, view):
    html = _watch_bar(role, is_admin=role == "admin", view=view, q="")

    assert "data-view" not in html and "view=list" not in html


def test_every_other_page_s_bar_has_the_way_back_to_watch_and_no_search():
    html = _render("admin", path="/downloads", is_admin=True)

    assert "watch-search" not in html and "space-meter" not in html
    (back,) = re.findall(r'<a class="nav-back" href="/watch">(.*?)</a>', _bar_of(html))
    assert back.endswith("Watch")


def test_a_page_can_fill_the_middle_and_the_end_of_the_desktop_bar():
    extra = (
        '{% extends "_nav.html" %}'
        "{% block nav_center %}<i>middle {{ request.url.path }}</i>{% endblock %}"
        "{% block nav_end %}<i>end</i>{% endblock %}"
    )
    env = _TEMPLATES.env.overlay(
        loader=ChoiceLoader([_TEMPLATES.env.loader, DictLoader({"extra.html": extra})])
    )
    html = env.from_string('{% include "extra.html" %}').render(request=_request("viewer"))

    bar = html.split('<header class="nav-glass">')[1].split("</header>")[0]
    places = [
        bar.index(part) for part in ('class="brand"', "middle /watch", ">end<", "profile-btn")
    ]
    assert places == sorted(places)


def test_log_out_is_a_posted_form():
    form = re.search(r'<form[^>]*action="/api/auth/logout"[^>]*>', _menu(_render("viewer"))).group(
        0
    )

    assert 'method="post"' in form


def test_one_button_at_every_width_names_the_person_and_opens_the_menu():
    html = _render("viewer", username="robin.ashdown")
    (button,) = _buttons(html)

    assert 'aria-haspopup="menu"' in button
    assert 'aria-expanded="false"' in button
    assert 'aria-label="Profile menu, robin.ashdown"' in button
    controlled = re.search(r'aria-controls="([^"]+)"', button).group(1)
    assert re.search(rf'id="{controlled}" role="menu"', html)


def test_the_ring_and_the_count_are_read_to_nobody_and_the_menu_s_note_waits_for_its_script():
    html = _render("viewer")

    for part in ("profile-ring", "profile-count"):
        assert re.search(rf'<span class="{part}" aria-hidden="true"></span>', html)
    assert re.search(r'id="profile-note" hidden>', _menu(html))


def test_the_phone_sheet_s_close_button_is_named_and_is_no_menu_item():
    before, menu = _menu(_render("viewer")).split('role="menu"')

    close = re.search(r'<button[^>]*class="profile-close"[^>]*>', before).group(0)
    assert 'aria-label="Close profile menu"' in close
    assert "profile-close" not in menu


def test_every_menu_item_is_a_menuitem_the_tab_key_passes_by():
    items = _menu(_render("admin")).split('role="menu"')[1]
    tags = re.findall(r"<(?:a|button)\b[^>]*>", items)

    assert len(tags) == 5
    assert all('role="menuitem"' in t and 'tabindex="-1"' in t for t in tags)


@pytest.mark.parametrize(
    ("active", "href", "title"),
    [
        ("settings", "/settings", "Settings"),
        ("history", "/history", "History"),
        ("account", "/account", "Profile"),
        ("downloads", "/downloads", "Downloads"),
    ],
)
def test_a_page_of_the_menu_marks_the_avatar_and_its_item(active, href, title):
    html = _render("admin", path=href, nav_active=active)

    for button in _buttons(html):
        assert 'class="profile-btn on"' in button
        assert f'aria-label="Profile menu, robin, {title} is open"' in button
    assert re.findall(r'href="([^"]+)" aria-current="page"', html) == [href]


def test_the_menu_shows_the_person_s_picture_wherever_it_shows_them_and_reads_it_to_nobody():
    html = _render("viewer", avatar_url="/api/avatars/3?v=a&b")

    pictures = re.findall(r'<span class="avatar[^>]*aria-hidden="true"><img ([^>]*)>', html)
    assert len(pictures) == 2
    assert all('src="/api/avatars/3?v=a&amp;b" alt=""' in picture for picture in pictures)


@pytest.mark.parametrize(
    ("name", "initials"),
    [
        ("robin", "R"),
        ("robin.ashdown", "RA"),
        ("Sam_van-Okoro", "SO"),
        ("sam.okoro@example.com", "SO"),
        ("...", "."),
        ("<b>", "&lt;"),
        (None, "P"),
    ],
)
def test_the_avatar_shows_initials(name, initials):
    html = _render("viewer", username=name)

    assert set(re.findall(r'<span class="avatar[^>]*>([^ ]*?)</span>', html)) == {initials}


def test_the_avatar_colour_follows_the_name():
    def colour(name: str) -> set[str]:
        return set(re.findall(r"avatar--(\d)", _render("viewer", username=name)))

    names = ["robin", "sam", "alex", "jo", "kim", "max", "pat", "lee"]
    assert all(len(colour(n)) == 1 for n in names)
    assert len({c for n in names for c in colour(n)}) > 2


@pytest.mark.parametrize(
    "page",
    [
        "login.html",
        "watch_player.html",
        "keep.html",
        "download.html",
        "force_password_change.html",
    ],
)
def test_signed_out_pages_and_the_player_have_no_menu(page):
    assert "_nav.html" not in (_TEMPLATE_DIR / page).read_text(encoding="utf-8")


@pytest.mark.parametrize("role", ["admin", "viewer", None])
def test_retired_pages_are_never_linked_or_labelled(role):
    html = _render(role)

    assert not _RETIRED_LINKS & set(_links(html))
    for label in ("Search", "Library", "Recommended"):
        assert label not in html


def test_unknown_role_gets_the_viewer_menu():
    assert "/settings" not in _links(_render("guest"))


@pytest.mark.parametrize("path", ["/watch", "/watch/show/abc", "/watch/play/42"])
def test_watch_pages_mark_the_way_home_without_nav_active(path):
    html = _render("viewer", path=path)

    assert re.search(
        r'<a class="brand" href="/watch" aria-label="mediaman, home" aria-current="page"', html
    )


def test_a_path_that_only_starts_like_watch_marks_nothing():
    html = _render("admin", path="/watching")

    assert "aria-current" not in html


def test_nav_renders_without_a_request():
    html = _TEMPLATES.env.get_template("_nav.html").render(nav_active="settings")

    assert re.search(r'href="/settings" aria-current="page"', html)


class TestAccountPage:
    @pytest.fixture
    def client(self, app_factory, authed_client, conn):
        app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
        return authed_client(app, conn)

    def test_requires_a_session(self, app_factory, conn):
        app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
        resp = TestClient(app).get("/account", follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers["location"] == "/login?next=%2Faccount"

    def test_shows_who_you_are_and_the_change_password_form(self, client):
        resp = client.get("/account")

        assert resp.status_code == 200
        assert "admin" in resp.text
        assert 'id="account-password-form"' in resp.text
        for field in ("account-old-password", "account-new-password", "account-confirm-password"):
            assert f'id="{field}"' in resp.text
        assert "/static/js/account.js" in resp.text
        assert "<script>" not in resp.text

    def test_shows_your_own_email_and_nobody_else_s(self, client, conn):
        insert_admin_user(conn, username="other")
        set_user_email(conn, "admin", "mine@example.com")
        set_user_email(conn, "other", "theirs@example.com")

        resp = client.get("/account")

        assert 'value="mine@example.com"' in resp.text
        assert "theirs@example.com" not in resp.text

    def test_keeps_the_place_links_to_the_plex_account_go_to(self, client):
        assert client.get("/account").text.count('id="plex"') == 1

    def test_a_viewer_sees_the_viewer_menu_with_profile_highlighted(
        self, app_factory, authed_client, conn
    ):
        app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})

        @app.middleware("http")
        async def _as_viewer(request, call_next):
            request.state.user_role = "viewer"
            return await call_next(request)

        resp = authed_client(app, conn).get("/account")

        assert "Viewer" in resp.text
        assert re.search(r'href="/account" aria-current="page"', resp.text)
        assert 'href="/settings"' not in resp.text
