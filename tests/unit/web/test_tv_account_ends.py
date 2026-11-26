"""What a password change, or an account's removal, does to a film it plays on a shared TV."""

from __future__ import annotations

import pytest

from mediaman.services.rate_limit import reset_all_limiters
from mediaman.services.tv import players
from mediaman.services.tv.players import Choice, Play, Players
from mediaman.web.auth.password_hash import _change_password as rotation
from mediaman.web.routes import tv as tv_routes
from mediaman.web.routes.users import router as users_router
from tests.helpers.factories import insert_admin_user

_KEY = "hash-of-the-tvs-token"
_NEW = "Harbour-glass-27"


@pytest.fixture(autouse=True)
def _cheap_hashes(monkeypatch):
    monkeypatch.setattr(rotation, "BCRYPT_ROUNDS", 4)
    monkeypatch.setattr("mediaman.web.auth.password_hash._user_crud.BCRYPT_ROUNDS", 4)


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
