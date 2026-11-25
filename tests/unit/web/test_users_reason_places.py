"""Each field of a form that says why beside the field has the element for it, on the page as it is rendered."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.web.routes.account import router as account_router
from mediaman.web.routes.settings import router as settings_router


class _Form(HTMLParser):
    """The fields of one form, each with what describes it, and the live regions in it."""

    def __init__(self, form: str) -> None:
        super().__init__()
        self._id, self._in = form, False
        self.fields: dict[str, str] = {}
        self.statuses: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._in = a.get("id") == self._id
        if self._in and tag == "input":
            self.fields[a["id"]] = a.get("aria-describedby", "")
        if self._in and a.get("role") == "status":
            self.statuses.add(a["id"])

    def handle_endtag(self, tag):
        if tag == "form":
            self._in = False


@pytest.mark.parametrize(
    ("router", "path", "form", "fields"),
    [
        (account_router, "/account", "account-password-form", 3),
        (settings_router, "/settings", "add-user-form", 2),
    ],
)
def test_each_field_that_can_be_refused_has_the_place_its_reason_is_said_in(
    app_factory, authed_client, conn, router, path, form, fields
):
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    app = app_factory(router, conn=conn, state_extras={"templates": templates})
    found = _Form(form)
    found.feed(authed_client(app, conn).get(path).text)

    assert len(found.fields) == fields
    for field, described_by in found.fields.items():
        assert f"{field}-why" in described_by.split()
        assert f"{field}-why" in found.statuses
