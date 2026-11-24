"""Tests for force_password_change route."""

from __future__ import annotations

import json
import re
import subprocess
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
HARNESS = Path(__file__).parent / "js" / "force_password_change_harness.js"
WEB = Path(__file__).parents[3] / "src" / "mediaman" / "web"


def _submit(typed: str, again: str, old: str = "the-old-one") -> dict:
    """What the page's own script does with a submit of these passwords, the cursor being
    in the new password."""
    result = subprocess.run(
        ["node", str(HARNESS), old, typed, again], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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


def test_new_passwords_that_differ_are_not_sent_and_the_cursor_goes_to_the_second() -> None:
    out = _submit("a-New-long-passw0rd!", "a-New-long-passw0rd?")
    # sending them would bring the page back with all three fields emptied
    assert (out["sent"], out["focused"]) == (False, "confirm_password")
    assert "match" in out["says"]
    # and the form is free for the next try at once
    assert out["busy"] is False


def test_an_empty_field_is_not_sent_and_the_cursor_goes_to_the_first_one() -> None:
    # sending it would bring the page back with what was typed in the others gone
    for old, typed, again, first in (
        ("", "a-New-long-passw0rd!", "a-New-long-passw0rd!", "old_password"),
        ("", "", "", "old_password"),
        ("the-old-one", "a-New-long-passw0rd!", "", "confirm_password"),
    ):
        out = _submit(typed, again, old)
        assert (out["sent"], out["focused"], out["busy"]) == (False, first, False)
        assert "every field" in out["error"]


def test_an_empty_field_is_said_beside_it_and_brought_into_view_with_the_word() -> None:
    # the box at the top can be far from the last field on a phone
    good = "a-New-long-passw0rd!"
    last, second, first = _submit(good, ""), _submit("", good), _submit(good, good, "")
    assert (last["says"], last["saysNew"]) == ("Please fill in this field.", "")
    assert (second["says"], second["saysNew"]) == ("", "Please fill in this field.")
    # the box is right above the first one
    assert (first["says"], first["saysNew"]) == ("", "")
    assert last["shown"] == ["confirm_password: login-field", "confirm_password: pw-match"]
    assert second["shown"] == ["new_password: login-field", "new_password: pw-strength-label"]
    assert first["shown"] == ["old_password: login-field", "old_password: error-msg"]


def test_the_word_on_passwords_that_differ_is_brought_into_view_with_its_field() -> None:
    out = _submit("a-New-long-passw0rd!", "a-New-long-passw0rd?")
    assert out["shown"] == ["confirm_password: login-field", "confirm_password: pw-match"]


def test_new_passwords_that_match_are_sent_once() -> None:
    out = _submit("a-New-long-passw0rd!", "a-New-long-passw0rd!")
    assert (out["sent"], out["busy"], out["focused"]) == (True, True, "new_password")
    assert out["error"] == ""


def test_the_sign_out_link_can_be_pressed_with_a_finger() -> None:
    css = (WEB / "static" / "css" / "_login.css").read_text()
    reach = re.search(r"\.security-note a::after \{[^}]*inset: -(\d+)px", css)
    # the link's own box is one 12px line, 15px tall
    assert reach and 15 + 2 * int(reach.group(1)) >= 44


def test_page_wording_fits_a_first_login_as_well_as_a_policy_failure():
    from fastapi.templating import Jinja2Templates
    from starlette.requests import Request

    template_dir = WEB / "templates"
    template = Jinja2Templates(directory=str(template_dir)).env.get_template(
        "force_password_change.html"
    )
    request = Request({"type": "http", "path": "/", "query_string": b"", "headers": []})

    html = template.render(request=request, username="new", policy="", error=None, issues=[])

    assert "Choose a new password to continue." in html
    assert "Choose a stronger password" not in html
    assert "no longer meets" not in html
    assert "Security update required" not in html
