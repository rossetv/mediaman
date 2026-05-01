"""Tests for force_password_change route."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.web.routes.force_password_change import (
    _FORCE_CHANGE_IP_LIMITER,
    _FORCE_CHANGE_LIMITER,
    router,
)


def _reset_limiters() -> None:
    """Force-change limiters are module-globals — reset between tests so the
    cross-test attempt count doesn't trip the per-IP cap."""
    _FORCE_CHANGE_LIMITER._attempts.clear()
    _FORCE_CHANGE_LIMITER._day_counts.clear()
    _FORCE_CHANGE_IP_LIMITER._attempts.clear()
    _FORCE_CHANGE_IP_LIMITER._day_counts.clear()


@pytest.fixture
def conn(tmp_path):
    db = init_db(str(tmp_path / "mediaman.db"))
    yield db
    db.close()


def _make_app(conn):
    app = FastAPI()
    app.include_router(router)
    app.state.db = conn
    set_connection(conn)
    mock_templates = MagicMock()
    mock_templates.TemplateResponse.side_effect = lambda req, tmpl, ctx: HTMLResponse(
        f"template:{tmpl}:error={ctx.get('error')}:issues={ctx.get('issues', [])}", 200
    )
    app.state.templates = mock_templates
    return app


SESSION_PATCH = "mediaman.web.routes.force_password_change.resolve_page_session"


class TestForceChangeGet:
    def test_redirects_when_no_session(self, conn):
        app = _make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)
        with patch(SESSION_PATCH) as mock_resolve:
            from fastapi.responses import RedirectResponse as RR

            mock_resolve.return_value = RR("/login", status_code=302)
            resp = client.get("/force-password-change", follow_redirects=False)
        assert resp.status_code == 302

    def test_renders_form_for_valid_session(self, conn):
        app = _make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)
        with patch(SESSION_PATCH, return_value=("admin", conn)):
            resp = client.get("/force-password-change")
        assert resp.status_code == 200
        assert "force_password_change.html" in resp.text


class TestForceChangePost:
    def setup_method(self):
        _reset_limiters()

    def _post(self, app, **form):
        client = TestClient(app, raise_server_exceptions=True)
        return client.post("/force-password-change", data=form)

    def test_missing_fields_renders_error(self, conn):
        app = _make_app(conn)
        with patch(SESSION_PATCH, return_value=("admin", conn)):
            resp = self._post(app, old_password="", new_password="", confirm_password="")
        assert "Please fill in every field" in resp.text

    def test_mismatched_passwords_renders_error(self, conn):
        app = _make_app(conn)
        with patch(SESSION_PATCH, return_value=("admin", conn)):
            resp = self._post(app, old_password="old", new_password="new1", confirm_password="new2")
        assert "don't match" in resp.text

    def test_policy_violation_renders_issues(self, conn):
        app = _make_app(conn)
        with patch(SESSION_PATCH, return_value=("admin", conn)):
            with patch(
                "mediaman.web.routes.force_password_change.password_issues",
                return_value=["Too short"],
            ):
                resp = self._post(
                    app, old_password="old", new_password="weak", confirm_password="weak"
                )
        assert "Too short" in resp.text

    def test_wrong_old_password_logs_security_event(self, conn):
        app = _make_app(conn)
        with patch(SESSION_PATCH, return_value=("admin", conn)):
            with patch(
                "mediaman.web.routes.force_password_change.password_issues", return_value=[]
            ):
                with patch(
                    "mediaman.web.routes.force_password_change.change_password", return_value=False
                ):
                    with patch(
                        "mediaman.web.routes.force_password_change.security_event"
                    ) as mock_event:
                        resp = self._post(
                            app,
                            old_password="wrong",
                            new_password="Str0ng!Pass",
                            confirm_password="Str0ng!Pass",
                        )
        assert "incorrect" in resp.text
        mock_event.assert_called_once()

    def test_success_rotates_session_and_redirects(self, conn):
        app = _make_app(conn)
        with patch(SESSION_PATCH, return_value=("admin", conn)):
            with patch(
                "mediaman.web.routes.force_password_change.password_issues", return_value=[]
            ):
                with patch(
                    "mediaman.web.routes.force_password_change.change_password", return_value=True
                ):
                    with patch("mediaman.auth.session.create_session", return_value="new-token"):
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
