"""Tests for force_password_change route."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mediaman.web.routes.force_password_change import (
    _FORCE_CHANGE_IP_LIMITER,
    _FORCE_CHANGE_LIMITER,
    router,
)
from tests.unit.web._css import px, style


def _reset_limiters() -> None:
    """Force-change limiters are module-globals — reset between tests so the
    cross-test attempt count doesn't trip the per-IP cap."""
    _FORCE_CHANGE_LIMITER.reset()
    _FORCE_CHANGE_IP_LIMITER.reset()


WEB = Path(__file__).parents[3] / "src" / "mediaman" / "web"
_LOGIN, _PASSWORD = "_login.css", "_password.css"
_FORCED = "(forced-colors: active)"


class TestForceChange:
    """Over HTTP with a real session; what each refusal says is in test_password_change_answers."""

    @pytest.fixture
    def client(self, app_factory, authed_client, conn, templates_stub):
        _reset_limiters()
        app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
        return authed_client(app, conn)

    def _post(self, client, old: str):
        new = "Harbour-glass-27"
        data = {"old_password": old, "new_password": new, "confirm_password": new}
        return client.post("/force-password-change", data=data, follow_redirects=False)

    def test_past_the_allowed_attempts_it_refuses_even_the_right_password(self, client):
        # an empty form counts as an attempt and sets off no lock on the account
        for _ in range(5):
            answer = client.post("/force-password-change", data={}).json()
            assert answer["field"] == "old_password"
        refused = self._post(client, old="password1234")
        assert refused.status_code == 200
        assert refused.json()["error"].startswith("Too many attempts")

    def test_signed_out_it_leads_to_sign_in(self, app_factory, conn, templates_stub):
        app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
        resp = TestClient(app).get("/force-password-change", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login")

    def test_a_wrong_current_password_is_a_security_event(self, client, conn):
        self._post(client, old="wrong")
        rows = conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'sec:password.force_change_failed'"
        ).fetchall()
        assert len(rows) == 1

    def test_success_signs_in_with_a_new_session_and_ends_the_old(self, client):
        old = client.cookies["session_token"]
        resp = self._post(client, old="password1234")
        assert (resp.status_code, resp.headers["location"]) == (302, "/")
        new = resp.cookies["session_token"]
        assert new != old
        for token, signed_in in ((new, True), (old, False)):
            client.cookies.set("session_token", token)
            page = client.get("/force-password-change", follow_redirects=False)
            assert (page.status_code == 200) is signed_in


def test_the_sign_out_link_can_be_pressed_with_a_finger() -> None:
    note = style(".security-note", _LOGIN)
    reach = px(style(".security-note a::after", _LOGIN)["inset"].split()[0])
    # an inline link's box is about one and a quarter of its font size tall
    assert px(note["font-size"]) * 1.25 + 2 * -reach >= 44


def test_a_field_the_browser_filled_in_keeps_the_eye_in_sight() -> None:
    # its pale blue is as light as the eye, and so is the text it puts on a dark one
    assert style(".login-page", _LOGIN)["color-scheme"] == "dark"
    filled = style(".pwd-box > .inp:autofill", _PASSWORD)
    assert filled["box-shadow"].startswith("inset") and "-webkit-text-fill-color" in filled


def test_the_system_s_colours_keep_the_eye_and_the_button_drawn() -> None:
    # a focused field's own rule is the stronger one, so it is named again there
    focused = ".pwd-box > .inp:focus ~ .pwd-eye .cut"
    assert style(focused, _PASSWORD, _FORCED)["stroke"] == "Canvas"
    width, kind, colour = style(".login-btn", _LOGIN)["border"].split()
    assert px(width) > 0 and kind == "solid" and colour == "transparent"


def test_a_long_unbroken_word_in_a_refusal_breaks_inside_the_card() -> None:
    assert style(".error-msg", _LOGIN)["overflow-wrap"] == "anywhere"


def test_a_sign_in_field_draws_one_ring_over_its_border_as_the_password_form_does() -> None:
    ring = style(".login-field input:focus-visible", _LOGIN)
    form = style(".pwd .inp.inp:focus-visible", _PASSWORD)
    assert ring["outline"] == form["outline"] and ring["outline-offset"] == form["outline-offset"]
    assert px(ring["outline-offset"]) < 0
    assert (
        style(".login-field input:focus-visible", _LOGIN, _FORCED)["outline-color"] == "Highlight"
    )


@pytest.mark.parametrize("page", ["login", "error", "force_password_change"])
def test_the_brand_is_the_drawn_name_that_keeps_to_its_width(page) -> None:
    html = (WEB / "templates" / f"{page}.html").read_text()
    brand = re.search(r'class="login-brand[^>]*>(.*?)</div>', html)
    assert brand and 'aria-label="mediaman"' in brand[1] and "wordmark" in brand[1]
    assert "<b>man</b>" not in brand[1]
    # Under a large text zoom it narrows with the card, and keeps the top bar's proportions.
    mark, bar = style(".login-brand .wordmark", _LOGIN), style(".wordmark", "_base.css")
    assert (mark["max-width"], mark["height"]) == ("100%", "auto")
    wide, tall = (float(v.split()[0]) for v in mark["aspect-ratio"].split("/"))
    assert wide / tall == pytest.approx(float(bar["width"][:-2]) / float(bar["height"][:-2]))
    assert style(".login-brand a", _LOGIN)["min-width"] == "0"
    # A grid item is no narrower than its content unless told, so the card would not narrow.
    assert style(".login-card", _LOGIN)["min-width"] == "0"


def test_the_top_bar_s_name_keeps_its_width() -> None:
    assert "max-width" not in style(".wordmark", "_base.css")


def test_an_open_form_s_fold_follows_its_height_in_safari() -> None:
    fold = ".setg-pg .setg-fold:has(.pwd):not(.is-shut, .is-moving, [hidden])"
    assert style(fold, _PASSWORD)["display"] == "flow-root"


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
