"""An error page keeps the nav of whoever is signed in, and stands without it when that cannot be told."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mediaman.app_factory import create_app
from mediaman.db import set_connection
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.middleware import viewer_guard

_HTML = {"Accept": "text/html"}
_NAV = '<nav class="nav-tabs"'


@pytest.fixture
def app(conn, secret_key):
    set_connection(conn)
    app = create_app()
    app.state.config = SimpleNamespace(secret_key=secret_key)
    app.state.db = conn
    return app


def _client(app, conn, role: str) -> TestClient:
    create_user(conn, role, "password1234", role=role, enforce_policy=False)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("session_token", create_session(conn, role))
    return client


def test_a_missing_page_keeps_the_nav_of_an_admin(app, conn):
    resp = _client(app, conn, "admin").get("/no/such/page", headers=_HTML)

    assert resp.status_code == 404
    assert _NAV in resp.text
    assert 'href="/settings"' in resp.text


def test_a_crashed_page_keeps_the_nav_of_an_admin(app, conn):
    app.add_api_route("/boom", lambda: 1 / 0)

    resp = _client(app, conn, "admin").get("/boom", headers=_HTML)

    assert resp.status_code == 500
    assert _NAV in resp.text


def test_a_viewer_is_shown_no_admin_link_on_an_error_page(app, conn):
    resp = _client(app, conn, "viewer").get("/watch/movie/not-a-key", headers=_HTML)

    assert resp.status_code == 404
    assert _NAV in resp.text
    assert 'href="/settings"' not in resp.text


@pytest.mark.parametrize("cookie", [None, "stale"])
def test_an_error_page_for_nobody_signed_in_has_no_nav(app, cookie):
    client = TestClient(app, cookies={"session_token": cookie} if cookie else None)

    resp = client.get("/no/such/page", headers=_HTML)

    assert resp.status_code == 404
    assert "nav-tabs" not in resp.text
    assert "media<b>man</b>" in resp.text


@pytest.mark.parametrize("read", ["resolve_cached_session", "get_user_role"])
def test_a_failed_session_or_role_read_still_shows_the_error_page(app, conn, monkeypatch, read):
    client = _client(app, conn, "admin")

    def fail(*_args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(viewer_guard, read, fail)

    resp = client.get("/no/such/page", headers=_HTML)

    assert resp.status_code == 500
    assert "Something went wrong" in resp.text
    assert "nav-tabs" not in resp.text
