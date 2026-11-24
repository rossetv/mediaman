"""The Profile page holds a person's own email, password and sign-ins; Settings only points to it."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.app_factory import _TEMPLATE_DIR
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

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
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


def test_settings_keeps_the_team_and_no_second_account_form(settings_html):
    page = _parse(settings_html)

    assert page.forms["add-user-form"] == ["btn-submit-add-user"]
    # the menu's Log out is the only other form
    assert set(page.forms) == {"add-user-form", None}


def test_settings_users_links_to_profile(settings_html):
    section = settings_html.split('id="setg-users"', 1)[1].split('id="user-list"', 1)[0]

    assert 'href="/account"' in section
