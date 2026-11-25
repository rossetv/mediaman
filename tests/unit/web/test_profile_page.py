"""The Profile page holds a person's own email, password and sign-ins; Settings only points to it."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.services.avatars import drawn
from mediaman.web.auth.reauth import grant_recent_reauth
from mediaman.web.repository import avatars
from mediaman.web.routes.account import router as account_router
from mediaman.web.routes.settings import router as settings_router

_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATE_DIR))


class _Page(HTMLParser):
    """Record each form's submit buttons, every id, and the forms' inputs by their id."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: dict[str, list[str]] = {}
        self._form: str | None = None
        self.ids: set[str] = set()
        self.inputs: dict[str, str | None] = {}
        self.hints: dict[str, set[str]] = {}
        self.scripts: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if a.get("data-strength"):
            key = a["data-strength"]
            self.hints[a["id"]] = {f"{key}-meter", f"{key}-label"}
        if a.get("data-match"):
            self.hints[a["id"]] = {a["data-match"], f"{a['id']}-match"}
        if tag == "script" and a.get("src"):
            self.scripts.add(a["src"].split("?")[0])
        if tag == "form":
            self._form = a.get("id")
            self.forms[self._form] = []
        elif tag == "button" and self._form and a.get("type") == "submit":
            self.forms[self._form].append(a.get("id"))
        elif tag == "input" and self._form and a.get("id"):
            self.inputs[a["id"]] = a.get("value")

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    return page


@pytest.fixture
def settings_html(app_factory, authed_client, conn) -> str:
    app = app_factory(settings_router, conn=conn, state_extras={"templates": _TEMPLATES})
    return authed_client(app, conn).get("/settings").text


def test_an_admin_manages_their_email_password_and_sign_ins_on_profile(
    app_factory, authed_client, conn
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    client = authed_client(app, conn)
    conn.execute("UPDATE admin_users SET email = 'admin@example.com'")
    conn.commit()

    page = _parse(client.get("/account").text)

    # each form is sent with Enter by its one submit button
    assert page.forms["account-email-form"] == ["account-submit-email"]
    assert page.inputs["account-email"] == "admin@example.com"
    assert page.forms["account-password-form"] == ["account-submit-password"]
    assert {"account-old-password", "account-new-password", "account-confirm-password"} <= set(
        page.inputs
    )
    assert "account-revoke-others" in page.ids


@pytest.mark.parametrize(
    ("router", "path", "fields"),
    [
        (account_router, "/account", {"account-new-password", "account-confirm-password"}),
        (settings_router, "/settings", {"new-user-password"}),
    ],
)
def test_new_password_fields_have_the_parts_their_hints_are_drawn_in(
    app_factory, authed_client, conn, router, path, fields
):
    app = app_factory(router, conn=conn, state_extras={"templates": _TEMPLATES})

    page = _parse(authed_client(app, conn).get(path).text)

    assert set(page.hints) == fields
    assert all(parts <= page.ids for parts in page.hints.values())
    assert "/static/js/password-strength.js" in page.scripts


def test_settings_keeps_the_team_and_no_second_account_form(settings_html):
    page = _parse(settings_html)

    assert page.forms["add-user-form"] == ["btn-submit-add-user"]
    # the menu's Log out is the only other form
    assert set(page.forms) == {"add-user-form", None}


def test_settings_users_links_to_profile(settings_html):
    section = settings_html.split('id="setg-users"', 1)[1].split('id="user-list"', 1)[0]

    assert 'href="/account"' in section


def test_what_the_ai_service_and_admins_get_is_in_sight_before_linking(
    app_factory, authed_client, conn
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    html = authed_client(app, conn).get("/account").text

    panel = html.split('data-plex-panel="unlinked"')[1].split("data-plex-panel")[0]
    in_sight = re.sub(r"<details.*?</details>", "", panel, flags=re.S)
    assert "AI service" in in_sight
    assert "Admins can see" in in_sight
    # said with the button too, which comes first
    terms = re.search(r'id="plex-start" aria-describedby="([\w-]+)"', panel)
    assert terms and "AI service" in panel.split(f'id="{terms[1]}"')[1].split("</ul>")[0]
    assert panel.count("AI service") == panel.count("Admins can see") == 1


def test_the_page_knows_how_long_this_sessions_password_is_good_for(
    app_factory, authed_client, conn
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    client = authed_client(app, conn)
    token = client.cookies.get("session_token")
    username = conn.execute("SELECT username FROM admin_users").fetchone()["username"]

    def good_for() -> int:
        found = re.search(r'data-password-good-for="(\d+)"', client.get("/account").text)
        assert found
        return int(found[1])

    grant_recent_reauth(conn, "another-session", username)
    assert good_for() == 0
    grant_recent_reauth(conn, token, "someone-else")
    assert good_for() == 0
    grant_recent_reauth(conn, token, username, window_seconds=100)
    assert 90 <= good_for() <= 100
    grant_recent_reauth(conn, token, username, window_seconds=9000)
    assert 290 <= good_for() <= 300
    grant_recent_reauth(conn, token, username, window_seconds=-1)
    assert good_for() == 0


def test_what_the_plex_card_waits_for_is_announced(app_factory, authed_client, conn):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    html = authed_client(app, conn).get("/account").text

    assert re.search(r'<p [^>]*id="plex-wait"[^>]*aria-live="polite"', html)


def test_the_page_shows_the_person_s_own_picture_and_the_ten_to_pick_from(
    app_factory, authed_client, conn
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    client = authed_client(app, conn)
    user_id = conn.execute("SELECT id FROM admin_users").fetchone()["id"]

    def shown() -> tuple[str, str, bool]:
        html = client.get("/account").text
        head = re.search(r'id="pic-open".*?<img src="([^"]+)"', html, re.S)
        dialog = re.search(r'<div[^>]*id="pic-dialog"[^>]*>', html)
        choice = re.search(r'data-choice="(\d*)"', dialog[0])
        assert len(re.findall(r'type="radio" name="picture"', html)) == drawn.COUNT
        assert f'data-default="{drawn.default_choice(user_id)}"' in dialog[0]
        return head[1], choice[1], "data-photo" in dialog[0]

    assert shown() == (drawn.url(drawn.default_choice(user_id)), "", False)
    avatars.set_drawn(conn, user_id, 3)
    assert shown() == (drawn.url(3), "3", False)
    avatars.set_upload(conn, user_id, "0123456789abcdef.png")
    assert shown() == (f"/api/avatars/{user_id}?v=0123456789ab", "", True)


def test_other_peoples_plex_links_are_not_on_an_admins_profile(app_factory, authed_client, conn):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})

    assert "Linked Plex accounts" not in authed_client(app, conn).get("/account").text


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_the_ai_part_is_there_for_everyone_and_hidden_until_the_ai_is_known_to_be(
    app_factory, authed_client, conn, role
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    client = authed_client(app, conn)
    conn.execute("UPDATE admin_users SET role = ?", (role,))
    conn.commit()

    html = client.get("/account").text

    assert re.search(r'<section[^>]*id="account-ai"[^>]*\bhidden>', html)
    switch = re.search(r'<button[^>]*id="account-history"[^>]*>', html).group(0)
    assert 'role="switch"' in switch and 'aria-labelledby="account-history-lbl"' in switch
    assert {"account-history-lbl", "account-clear-searches"} <= _parse(html).ids


@pytest.mark.parametrize("page", ["profile", "settings"])
def test_all_that_openai_is_sent_is_said_beside_its_switch(
    app_factory, authed_client, conn, settings_html, page
):
    html = settings_html
    if page == "profile":
        app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
        html = authed_client(app, conn, username="second").get("/account").text
    start = 'id="account-history-sub">' if page == "profile" else ">AI recommendations</div>"
    said = html.split(start)[1].split("</div>")[0]

    for part in (
        "account name",
        "recent picks",
        "Plex stars",
        "suggestions to choose from",
        "question",
        "more like",
        "narrow",
        "searches, ratings, requests",
        "searches, plays and ratings",
        "daily phrases to try",
    ):
        assert part in said, part


@pytest.mark.parametrize(("role", "lists"), [("admin", 2), ("viewer", 1)])
def test_only_an_admins_profile_has_a_list_of_other_peoples_tvs(
    app_factory, authed_client, conn, role, lists
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})

    @app.middleware("http")
    async def as_role(request, call_next):
        request.state.user_role = role
        return await call_next(request)

    html = authed_client(app, conn).get("/account").text

    # hidden until there are TVs
    assert len(re.findall(r'<section[^>]*id="account-tvs[^>]*\bhidden>', html)) == lists
    assert ('id="account-tvs-all"' in html) == (role == "admin")
