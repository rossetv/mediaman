"""A typed name finds its account whatever the capitals, unless two names differ only in them."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth import cli as cli_mod
from mediaman.web.auth.password_hash import UserExistsError, create_user, list_users
from mediaman.web.auth.session_store import validate_session
from mediaman.web.routes.auth import _limiter, _success_limiter
from mediaman.web.routes.auth import router as auth_router
from mediaman.web.routes.users import router as users_router
from mediaman.web.routes.users.rate_limits import _USER_CREATE_LIMITER

_PASSWORD = "password1234"


@pytest.fixture(autouse=True)
def _clear_limiters():
    _limiter.reset()
    _success_limiter.reset()
    _USER_CREATE_LIMITER.reset()


@pytest.fixture
def client(app_factory, conn, templates_stub):
    app = app_factory(auth_router, conn=conn, state_extras={"templates": templates_stub})
    return TestClient(app, raise_server_exceptions=True)


def _insert_unchecked(conn, username: str, password: str) -> None:
    """Add a user the way an older version could, beside a name differing only in capitals."""
    create_user(conn, f"tmp-{username}", password, enforce_policy=False)
    conn.execute(
        "UPDATE admin_users SET username = ? WHERE username = ?", (username, f"tmp-{username}")
    )
    conn.commit()


def _sign_in(client, username: str, password: str):
    with patch("mediaman.web.routes.auth.is_request_secure", return_value=False):
        return client.post(
            "/login", data={"username": username, "password": password}, follow_redirects=False
        )


def test_sign_in_with_other_capitals_opens_a_session_under_the_stored_name(client, conn):
    create_user(conn, "Admin", _PASSWORD, enforce_policy=False)
    resp = _sign_in(client, "aDMIN", _PASSWORD)
    assert resp.status_code == 302
    token = resp.cookies["session_token"]
    assert validate_session(conn, token, user_agent="testclient", client_ip="testclient") == "Admin"


def test_capital_variants_share_one_lock(client, conn):
    create_user(conn, "admin", _PASSWORD, enforce_policy=False)
    for typed in ("Admin", "ADMIN", "aDmin", "admiN", "AdMiN"):
        assert _sign_in(client, typed, "wrong-guess").status_code == 200
    _limiter.reset()
    resp = _sign_in(client, "admin", _PASSWORD)
    assert "session_token" not in resp.cookies


def test_names_differing_only_in_capitals_need_the_exact_name(client, conn):
    _insert_unchecked(conn, "Viewer", "first-password")
    _insert_unchecked(conn, "viewer", "second-password")
    assert "session_token" not in _sign_in(client, "VIEWER", "first-password").cookies
    assert "session_token" not in _sign_in(client, "VIEWER", "second-password").cookies
    assert "session_token" in _sign_in(client, "Viewer", "first-password").cookies
    assert "session_token" in _sign_in(client, "viewer", "second-password").cookies


def test_an_unknown_name_gets_the_usual_refusal_and_its_text_back(client, conn):
    create_user(conn, "admin", _PASSWORD, enforce_policy=False)
    resp = _sign_in(client, "Nobody", _PASSWORD)
    assert resp.status_code == 200
    ctx = resp.json()
    assert ctx["error"] == "Invalid username or password."
    assert ctx["username"] == "Nobody"


def test_a_failed_sign_in_gives_back_the_text_typed_not_the_stored_name(client, conn):
    create_user(conn, "admin", _PASSWORD, enforce_policy=False)
    assert _sign_in(client, "ADMIN", "wrong-guess").json()["username"] == "ADMIN"


def test_creating_a_name_that_differs_only_in_capitals_is_refused(conn):
    create_user(conn, "viewer", _PASSWORD, enforce_policy=False)
    with pytest.raises(UserExistsError, match="already exists"):
        create_user(conn, "Viewer", _PASSWORD, enforce_policy=False)
    assert [user["username"] for user in list_users(conn)] == ["viewer"]


def test_the_users_page_refuses_a_name_that_differs_only_in_capitals(
    app_factory, authed_client, conn
):
    client = authed_client(app_factory(users_router, conn=conn), conn, with_reauth=True)
    body = {"username": "dupuser", "password": "ValidPass!99"}
    assert client.post("/api/users", json=body).status_code == 200
    resp = client.post("/api/users", json={**body, "username": "DupUser"})
    assert resp.status_code == 409
    assert "already exists" in resp.json()["message"]


def test_the_create_user_command_refuses_a_name_that_differs_only_in_capitals(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "0123456789abcdef" * 4)
    monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(tmp_path))
    args = ["mediaman-create-user", "--password", "Correct-Horse-Battery-9!", "--username"]
    monkeypatch.setattr(sys, "argv", [*args, "alice"])
    cli_mod.create_user_cli()
    monkeypatch.setattr(sys, "argv", [*args, "ALICE"])
    with pytest.raises(SystemExit) as exc:
        cli_mod.create_user_cli()
    assert exc.value.code == 1
    assert "already exists" in capsys.readouterr().err
