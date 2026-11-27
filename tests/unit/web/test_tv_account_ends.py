"""What a password change, a weak password at sign-in, or an account's removal, does to a film it plays on a shared TV."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import set_connection
from mediaman.main import create_app
from mediaman.services.rate_limit import reset_all_limiters
from mediaman.services.tv import players
from mediaman.services.tv.players import Choice, Play, Players
from mediaman.web.routes import tv as tv_routes
from mediaman.web.routes.auth import _limiter
from mediaman.web.routes.auth import router as auth_router
from mediaman.web.routes.force_password_change import (
    _FORCE_CHANGE_IP_LIMITER,
    _FORCE_CHANGE_LIMITER,
)
from mediaman.web.routes.force_password_change import router as force_router
from mediaman.web.routes.users import router as users_router
from tests.helpers.factories import insert_admin_user

_KEY = "hash-of-the-tvs-token"
_NEW = "Harbour-glass-27"


@pytest.fixture
def board(monkeypatch) -> Players:
    board = Players(clock=lambda: 1000.0)
    monkeypatch.setattr(players, "_players", board)
    return board


@pytest.fixture
def admin(app_factory, authed_client, conn, board):
    reset_all_limiters()
    app = app_factory(users_router, tv_routes.router, conn=conn)
    client = authed_client(app, conn, with_reauth=True)
    insert_admin_user(conn, username="vic", role="viewer")
    conn.execute(
        "INSERT INTO tvs (owner_id, kind, name, token_hash, created_at)"
        " SELECT id, 'page', 'Den', ?, '2026' FROM admin_users WHERE username = 'admin'",
        (_KEY,),
    )
    conn.execute(
        "INSERT INTO tv_people (tv_id, user_id, paired_at)"
        " SELECT tvs.id, u.id, '2027' FROM tvs, admin_users u WHERE u.username = 'vic'"
    )
    conn.commit()
    return client


def _plays(board, conn, user):
    (user_id,) = conn.execute("SELECT id FROM admin_users WHERE username = ?", (user,)).fetchone()
    board.poll(_KEY, ack=0, active=True, report=None)
    play = Play(user, user_id, "101", {"title": "Film"}, Choice(), 0, 0, 0)
    board.begin(_KEY, play, over=None)


def test_a_password_change_ends_the_persons_film_and_says_so(admin, board, conn):
    _plays(board, conn, "admin")
    changed = admin.post(
        "/api/users/change-password",
        json={"old_password": "password1234", "new_password": _NEW},
        headers={"origin": "http://testserver"},
    )
    assert changed.status_code == 200, changed.text
    assert (board.view(_KEY)["play"], board.view(_KEY)["last"]["stopped_by"]) == (None, "admin")
    (tv_id,) = conn.execute("SELECT id FROM tvs").fetchone()
    admin.cookies.set("session_token", changed.cookies["session_token"])
    gone = admin.get(f"/api/tvs/{tv_id}")
    assert (gone.status_code, gone.json()["reason"], gone.json()["stopped"]) == (
        410,
        "account",
        "Film",
    )


def test_removing_an_account_ends_its_film_as_stopped_by_the_admin(admin, board, conn):
    _plays(board, conn, "vic")
    (vic,) = conn.execute("SELECT id FROM admin_users WHERE username = 'vic'").fetchone()
    assert admin.delete(f"/api/users/{vic}").status_code == 200
    assert (board.view(_KEY)["play"], board.view(_KEY)["last"]["stopped_by"]) == (None, "admin")


def _unpaired_with_the_film(client, conn, token):
    (tv_id,) = conn.execute("SELECT id FROM tvs").fetchone()
    client.cookies.set("session_token", token)
    gone = client.get(f"/api/tvs/{tv_id}")
    return gone.status_code, gone.json().get("reason"), gone.json().get("stopped")


def test_a_weak_password_at_sign_in_ends_the_persons_film_and_the_tv_route_says_why(
    app_factory, admin, board, conn, templates_stub
):
    _limiter.reset()
    _plays(board, conn, "admin")
    app = app_factory(auth_router, tv_routes.router, conn=conn)
    app.state.templates = templates_stub
    client = TestClient(app)
    signed_in = client.post(
        "/login",
        data={"username": "admin", "password": "password1234"},
        follow_redirects=False,
    )
    assert signed_in.status_code == 302, signed_in.text
    assert (board.view(_KEY)["play"], board.view(_KEY)["last"]["stopped_by"]) == (None, "admin")
    token = signed_in.cookies["session_token"]
    assert _unpaired_with_the_film(client, conn, token) == (410, "account", "Film")


def test_after_a_weak_password_at_sign_in_the_phone_is_sent_to_change_it(
    admin, board, conn, secret_key, templates_stub
):
    # The whole app: its middlewares answer the phone before the TV route can.
    _limiter.reset()
    _plays(board, conn, "admin")
    app = create_app()
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    app.state.templates = templates_stub
    set_connection(conn)
    phone = TestClient(app, headers={"Origin": "http://testserver"})
    signed_in = phone.post(
        "/login",
        data={"username": "admin", "password": "password1234"},
        follow_redirects=False,
    )
    assert signed_in.status_code == 302, signed_in.text
    assert (board.view(_KEY)["play"], board.view(_KEY)["last"]["stopped_by"]) == (None, "admin")
    phone.cookies.set("session_token", signed_in.cookies["session_token"])
    (tv_id,) = conn.execute("SELECT id FROM tvs").fetchone()
    read = phone.get(f"/api/tvs/{tv_id}", follow_redirects=False)
    assert (read.status_code, read.headers["location"]) == (302, "/force-password-change")
    steered = phone.post(f"/api/tvs/{tv_id}/commands", json={"kind": "pause", "play_id": "x"})
    assert (steered.status_code, steered.json()["redirect"]) == (403, "/force-password-change")


def test_a_forced_password_change_ends_the_persons_film_and_unpairs_them(
    app_factory, admin, board, conn, templates_stub
):
    # Guards the defensive read for a state the app prevents: a flagged person is never paired.
    conn.execute("UPDATE admin_users SET must_change_password = 1 WHERE username = 'admin'")
    conn.execute(
        "INSERT INTO tv_people (tv_id, user_id, paired_at)"
        " SELECT tvs.id, u.id, '2026' FROM tvs, admin_users u WHERE u.username = 'admin'"
    )
    conn.commit()
    _FORCE_CHANGE_LIMITER.reset()
    _FORCE_CHANGE_IP_LIMITER.reset()
    _plays(board, conn, "admin")
    app = app_factory(force_router, tv_routes.router, conn=conn)
    app.state.templates = templates_stub
    client = TestClient(app, cookies=dict(admin.cookies))
    changed = client.post(
        "/force-password-change",
        data={"old_password": "password1234", "new_password": _NEW, "confirm_password": _NEW},
        follow_redirects=False,
    )
    assert changed.status_code == 302, changed.text
    assert (board.view(_KEY)["play"], board.view(_KEY)["last"]["stopped_by"]) == (None, "admin")
    token = changed.cookies["session_token"]
    assert _unpaired_with_the_film(client, conn, token) == (410, "account", "Film")
