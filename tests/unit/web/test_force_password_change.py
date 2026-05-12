"""Tests for force_password_change route."""

from __future__ import annotations

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
        assert resp.headers["location"] == "/"
