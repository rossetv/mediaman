"""The settings and account pages: account forms, password fields and the section rail."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.web.routes.account import router as account_router
from mediaman.web.routes.settings import router as settings_router

_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATE_DIR))


class _Page(HTMLParser):
    """Record each form's submit buttons, the account password inputs, and the rail links.

    Saved service secrets (autocomplete off) have their own reveal button and are left out.
    """

    def __init__(self) -> None:
        super().__init__()
        self.forms: dict[str, list[str]] = {}
        self._form: str | None = None
        self.passwords: list[dict] = []
        self.rail: dict[str, list[str]] = {}
        self._rail_link: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "form":
            self._form = a.get("id")
            self.forms[self._form] = []
        elif tag == "button" and self._form and a.get("type") == "submit":
            self.forms[self._form].append(a.get("id"))
        elif tag == "input" and a.get("type") == "password" and a.get("autocomplete") != "off":
            self.passwords.append(a)
        elif tag == "a" and "setg-rail-item" in classes:
            self._rail_link = a["href"]
            self.rail[self._rail_link] = []
        elif tag == "span" and self._rail_link:
            self.rail[self._rail_link].append(" ".join(classes))

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "a":
            self._rail_link = None


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    return page


@pytest.fixture
def settings_html(app_factory, authed_client, conn) -> str:
    app = app_factory(settings_router, conn=conn, state_extras={"templates": _TEMPLATES})
    return authed_client(app, conn).get("/settings").text


@pytest.fixture
def account_html(app_factory, authed_client, conn) -> str:
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    return authed_client(app, conn).get("/account").text


def test_account_and_team_forms_submit_with_enter(settings_html):
    forms = _parse(settings_html).forms

    assert forms["self-password-form"] == ["btn-submit-password"]
    assert forms["self-email-form"] == ["btn-submit-email"]
    assert forms["add-user-form"] == ["btn-submit-add-user"]


@pytest.mark.parametrize("page", ["settings_html", "account_html"])
def test_every_password_field_can_be_shown(page, request):
    html = request.getfixturevalue(page)
    passwords = _parse(html).passwords

    assert passwords
    assert [p["id"] for p in passwords if "data-show-password" not in p] == []
    assert '<script src="/static/js/show-password.js" defer></script>' in html


def test_integrations_rail_item_carries_no_fixed_count(settings_html):
    rail = _parse(settings_html).rail

    assert "#setg-integrations" in rail
    assert [c for c in rail["#setg-integrations"] if "rail-count" in c] == []
