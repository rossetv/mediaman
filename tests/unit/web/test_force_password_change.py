"""Tests for force_password_change route."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mediaman.web.routes.force_password_change import (
    _FORCE_CHANGE_IP_LIMITER,
    _FORCE_CHANGE_LIMITER,
    router,
)


def _reset_limiters() -> None:
    """Force-change limiters are module-globals — reset between tests so the
    cross-test attempt count doesn't trip the per-IP cap."""
    _FORCE_CHANGE_LIMITER.reset()
    _FORCE_CHANGE_IP_LIMITER.reset()


SESSION_PATCH = "mediaman.web.routes.force_password_change.resolve_page_session"
WEB = Path(__file__).parents[3] / "src" / "mediaman" / "web"


class TestForceChangeGet:
    def test_redirects_when_no_session(self, app_factory, conn, templates_stub):
        app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app, raise_server_exceptions=True)
        with patch(SESSION_PATCH) as mock_resolve:
            from fastapi.responses import RedirectResponse as RR

            mock_resolve.return_value = RR("/login", status_code=302)
            resp = client.get("/force-password-change", follow_redirects=False)
        assert resp.status_code == 302

    def test_renders_form_for_valid_session(self, app_factory, conn, templates_stub):
        app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
        client = TestClient(app, raise_server_exceptions=True)
        with patch(SESSION_PATCH, return_value=("admin", conn)):
            resp = client.get("/force-password-change")
        assert resp.status_code == 200


class TestForceChangePost:
    @pytest.fixture(autouse=True)
    def _reset_limiters_fixture(self):
        _reset_limiters()

    def _post(self, app, **form):
        client = TestClient(app, raise_server_exceptions=True)
        return client.post("/force-password-change", data=form)

    def test_missing_fields_renders_error(self, app_factory, conn, templates_stub):
        app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
        with patch(SESSION_PATCH, return_value=("admin", conn)):
            resp = self._post(app, old_password="", new_password="", confirm_password="")
        assert "Please fill in every field" in resp.text

    def test_mismatched_passwords_renders_error(self, app_factory, conn, templates_stub):
        app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
        with patch(SESSION_PATCH, return_value=("admin", conn)):
            resp = self._post(app, old_password="old", new_password="new1", confirm_password="new2")
        assert "don't match" in resp.text

    def test_policy_violation_renders_issues(self, app_factory, conn, templates_stub):
        app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
        with (
            patch(SESSION_PATCH, return_value=("admin", conn)),
            patch(
                "mediaman.web.routes.force_password_change.password_issues",
                return_value=["Too short"],
            ),
        ):
            resp = self._post(app, old_password="old", new_password="weak", confirm_password="weak")
        assert "Too short" in resp.text

    def test_wrong_old_password_logs_security_event(self, app_factory, conn, templates_stub):
        app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
        with (
            patch(SESSION_PATCH, return_value=("admin", conn)),
            patch("mediaman.web.routes.force_password_change.password_issues", return_value=[]),
            patch("mediaman.web.routes.force_password_change.change_password", return_value=False),
            patch("mediaman.web.routes.force_password_change.security_event") as mock_event,
        ):
            resp = self._post(
                app,
                old_password="wrong",
                new_password="Str0ng!Pass",
                confirm_password="Str0ng!Pass",
            )
        assert "incorrect" in resp.text
        mock_event.assert_called_once()

    def test_success_rotates_session_and_redirects(self, app_factory, conn, templates_stub):
        app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
        with (
            patch(SESSION_PATCH, return_value=("admin", conn)),
            patch("mediaman.web.routes.force_password_change.password_issues", return_value=[]),
            patch("mediaman.web.routes.force_password_change.change_password", return_value=True),
            patch("mediaman.web.auth.session_store.create_session", return_value="new-token"),
        ):
            client = TestClient(app, raise_server_exceptions=True)
            resp = client.post(
                "/force-password-change",
                data={
                    "old_password": "old",
                    "new_password": "Strong!Pass1",
                    "confirm_password": "Strong!Pass1",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/watch"


def test_the_sign_out_link_can_be_pressed_with_a_finger() -> None:
    css = (WEB / "static" / "css" / "_login.css").read_text()
    reach = re.search(r"\.security-note a::after \{[^}]*inset: -(\d+)px", css)
    # the link's own box is one 12px line, 15px tall
    assert reach and 15 + 2 * int(reach.group(1)) >= 44


def _rule(css: str, selector: str) -> str:
    found = re.search(rf"^\s*{re.escape(selector)} \{{([^}}]*)\}}", css, re.M)
    assert found, selector
    return found[1]


def test_a_field_the_browser_filled_in_keeps_the_eye_in_sight() -> None:
    login = (WEB / "static" / "css" / "_login.css").read_text()
    password = (WEB / "static" / "css" / "_password.css").read_text()
    # its pale blue is as light as the eye, and so is the text it puts on a dark one
    assert "color-scheme: dark" in _rule(login, ".login-page")
    filled = _rule(password, ".pwd-box > .inp:autofill")
    assert "box-shadow: inset" in filled and "-webkit-text-fill-color" in filled


def test_the_system_s_colours_keep_the_eye_and_the_button_drawn() -> None:
    login = (WEB / "static" / "css" / "_login.css").read_text()
    password = (WEB / "static" / "css" / "_password.css").read_text()
    forced = password.split("@media (forced-colors: active)", 1)[1]
    # a focused field's own rule is the stronger one
    assert ".pwd-box > .inp:focus ~ .pwd-eye .cut { stroke: Canvas; }" in forced
    assert "border: 1px solid transparent" in _rule(login, ".login-btn")


def test_a_long_unbroken_word_in_a_refusal_breaks_inside_the_card() -> None:
    login = (WEB / "static" / "css" / "_login.css").read_text()
    assert "overflow-wrap: anywhere" in _rule(login, ".error-msg")


def test_a_sign_in_field_draws_one_ring_over_its_border_as_the_password_form_does() -> None:
    login = (WEB / "static" / "css" / "_login.css").read_text()
    ring = _rule(login, ".login-field input:focus-visible")
    assert "outline: 2px solid var(--accent)" in ring and "outline-offset: -1px" in ring
    forced = login.split("@media (forced-colors: active)", 1)[1]
    assert ".login-field input:focus-visible { outline-color: Highlight; }" in forced


@pytest.mark.parametrize("page", ["login", "error", "force_password_change"])
def test_the_brand_is_the_drawn_name_that_keeps_to_its_width(page) -> None:
    html = (WEB / "templates" / f"{page}.html").read_text()
    brand = re.search(r'class="login-brand[^>]*>(.*?)</div>', html)
    assert brand and 'aria-label="mediaman"' in brand[1] and "wordmark" in brand[1]
    assert "<b>man</b>" not in brand[1]
    # Under a large text zoom it narrows with the card, and keeps its proportions.
    mark = _rule((WEB / "static" / "css" / "_base.css").read_text(), ".wordmark")
    assert "max-width: 100%" in mark and "height: auto" in mark
    assert "aspect-ratio: 4.4316 / 1.0211" in mark
    link = _rule((WEB / "static" / "css" / "_login.css").read_text(), ".login-brand a")
    assert "min-width: 0" in link


def test_an_open_form_s_fold_follows_its_height_in_safari() -> None:
    password = (WEB / "static" / "css" / "_password.css").read_text()
    fold = ".setg-pg .setg-fold:has(.pwd):not(.is-shut, .is-moving, [hidden])"
    assert "display: flow-root" in _rule(password, fold)


def _page(**context) -> str:
    from fastapi.templating import Jinja2Templates
    from starlette.requests import Request

    template = Jinja2Templates(directory=str(WEB / "templates")).env.get_template(
        "force_password_change.html"
    )
    request = Request({"type": "http", "path": "/", "query_string": b"", "headers": []})
    return template.render(
        {"request": request, "username": "new", "policy": "", "error": None, "issues": []}
        | {"field": None, "rules": [], "locked": False}
        | context
    )


def test_page_wording_fits_a_first_login_as_well_as_a_policy_failure():
    html = _page()

    assert "Choose a new password to continue." in html
    assert "Choose a stronger password" not in html
    assert "no longer meets" not in html
    assert "Security update required" not in html


def _why(html: str, field: str) -> str:
    """The words under a field, which are announced and its field marked when there are any."""
    said = re.search(rf'id="{field}-why" role="(\w+)">(.*?)</div>', html, re.S)
    marked = re.search(rf'id="{field}" [^>]*aria-invalid="true"', html)
    assert said and (said[1] == "alert") == bool(said[2]) == bool(marked)
    return said[2]


@pytest.mark.parametrize("field", ["old_password", "new_password", "confirm_password"])
def test_a_refusal_is_under_its_own_field_without_scripts(field):
    html = _page(error="No <b>", field=field)

    others = {"old_password", "new_password", "confirm_password"} - {field}
    assert _why(html, field) == "No &lt;b&gt;"
    assert [_why(html, other) for other in others] == ["", ""]


@pytest.mark.parametrize(
    ("context", "focused"),
    [
        ({}, "old_password"),
        ({"error": "No", "field": "old_password"}, "old_password"),
        ({"error": "No", "field": "new_password"}, "new_password"),
        ({"error": "No", "field": "confirm_password"}, "confirm_password"),
        ({"issues": ["Too short."], "rules": ["length"]}, "new_password"),
        ({"error": "Too many attempts"}, "old_password"),
    ],
)
def test_the_page_opens_on_the_field_it_refused(context, focused):
    assert re.findall(r'id="(\w+)"[^>]*\bautofocus\b', _page(**context)) == [focused]


def test_a_limit_is_about_the_whole_form():
    html = _page(error="Too many attempts")

    assert re.search(r'id="fpc-result" role="alert">Too many attempts<', html)
    assert "aria-invalid" not in html


def _broken(html: str) -> list[str]:
    return re.findall(r'class="pwd-rule is-bad" data-rule="(\w+)"', html)


def test_the_rules_a_password_broke_are_marked_where_they_stand_without_scripts():
    common = "This password is on the common-password list."
    one = _page(issues=[common], field="new_password", rules=["common"])
    several = _page(
        issues=["Must not contain the username.", common],
        field="new_password",
        rules=["username", "common"],
    )

    assert (_why(one, "new_password"), _broken(one)) == (common, ["common"])
    assert _broken(several) == ["guess", "common"]
    assert "It has your username in it" in several
    assert "strength policy" in _why(several, "new_password")
    # as the script takes it up
    assert 'data-refused="username common"' in several
    assert _broken(_page()) == []


def test_a_rule_with_no_line_of_its_own_is_listed_under_the_field():
    html = _page(issues=["Some new rule.", "Other."], field="new_password", rules=["other", "mix"])

    assert re.findall(r"<li>([^<]*)</li>", _why(html, "new_password")) == ["Some new rule."]
    assert _broken(html) == ["mix"]


def test_the_page_works_without_scripts_and_holds_one_form_for_a_password_manager():
    html = _page()
    form = html.split("<form ", 1)[1].split("</form>", 1)[0]
    inputs = re.findall(r"<input ([^>]*)>", form)
    fields = {
        re.search(r'name="(\w+)"', tag)[1]: re.search(r'autocomplete="([\w-]+)"', tag)[1]
        for tag in inputs
    }

    assert html.count("<form ") == 1
    assert fields == {
        "username": "username",
        "old_password": "current-password",
        "new_password": "new-password",
        "confirm_password": "new-password",
    }
    assert 'method="post" action="/force-password-change"' in form
    # what password-form.js wires, and the field it fills as the form is sent
    assert "data-password-form" in form.split(">", 1)[0]
    assert 'data-again="new_password"' in inputs[-1]
    # the script alone takes the second field away
    assert not any(re.search(r"\bhidden\b(?!=)", tag) for tag in inputs if "aria-hidden" not in tag)
