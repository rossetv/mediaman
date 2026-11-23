"""The viewer guard: role on the request, default deny, and where sign-in lands."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.db import set_connection
from mediaman.main import create_app
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session

_ORIGIN = {"origin": "http://testserver"}


def _set_role(conn, username: str, role: str) -> None:
    conn.execute("UPDATE admin_users SET role = ? WHERE username = ?", (role, username))
    conn.commit()


def test_a_viewer_gets_its_role_on_the_request(guarded_app, conn, viewer_client):
    app, reached = guarded_app
    client = viewer_client(app, conn)

    assert client.get("/search").status_code == 204
    assert reached == [("GET", "/search", "viewer")]


def test_an_admin_gets_its_role_on_the_request(guarded_app, conn, authed_client):
    app, reached = guarded_app
    client = authed_client(app, conn)

    assert client.get("/settings").status_code == 204
    assert reached == [("GET", "/settings", "admin")]


@pytest.mark.parametrize("path", ["/settings", "/"])
def test_a_viewer_is_redirected_from_an_admin_page_to_watch(guarded_app, conn, viewer_client, path):
    app, reached = guarded_app
    client = viewer_client(app, conn)

    resp = client.get(path, follow_redirects=False)

    assert (resp.status_code, resp.headers["location"]) == (303, "/watch")
    assert reached == []


def test_a_viewer_cannot_abandon_a_download(guarded_app, conn, viewer_client):
    app, reached = guarded_app
    client = viewer_client(app, conn)

    resp = client.post(
        "/api/downloads/1/abandon", json={"kind": "movie", "seasons": []}, headers=_ORIGIN
    )

    assert (resp.status_code, resp.json()) == (403, {"error": "forbidden"})
    assert reached == []


def test_a_role_other_than_admin_or_viewer_is_refused(guarded_app, conn, authed_client):
    app, reached = guarded_app
    client = authed_client(app, conn, username="odd")
    _set_role(conn, "odd", "superuser")

    resp = client.get("/settings", follow_redirects=False)

    assert resp.headers["location"] == "/watch"
    assert reached == []


def test_a_demotion_applies_on_the_next_request(guarded_app, conn, authed_client):
    app, reached = guarded_app
    client = authed_client(app, conn)
    assert client.get("/settings").status_code == 204

    _set_role(conn, "admin", "viewer")
    resp = client.get("/settings", follow_redirects=False)

    assert resp.headers["location"] == "/watch"
    assert reached == [("GET", "/settings", "admin")]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/watchx"),
        ("GET", "/api/watchx/search"),
        ("GET", "/api/watch/%2e%2e/settings"),
        ("PUT", "/api/watch/%2e%2e/settings"),
        ("POST", "/api/watch/%2e%2e/users"),
        ("GET", "/api/search/download"),
    ],
)
def test_a_path_that_only_looks_allowed_is_refused(guarded_app, conn, viewer_client, method, path):
    app, reached = guarded_app
    client = viewer_client(app, conn)

    resp = client.request(method, path, headers=_ORIGIN, follow_redirects=False)

    assert resp.status_code in (303, 403)
    assert reached == []


def test_a_request_without_a_session_passes_untouched(guarded_app):
    app, reached = guarded_app

    resp = TestClient(app).get("/settings")

    assert resp.status_code == 204
    assert reached == [("GET", "/settings", None)]


def test_a_flagged_viewer_is_sent_to_change_its_password_first(guarded_app, conn):
    app, reached = guarded_app
    create_user(
        conn, "v", "password1234", enforce_policy=False, role="viewer", must_change_password=True
    )
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "v"))

    resp = client.get("/settings", follow_redirects=False)

    assert resp.headers["location"] == "/force-password-change"


@pytest.fixture
def real_app(conn):
    set_connection(conn)
    return create_app()


@pytest.mark.parametrize(("role", "landing"), [("viewer", "/watch"), ("admin", "/")])
def test_signing_in_lands_by_role(real_app, conn, role, landing):
    create_user(conn, "someone", "Correct-Horse-9-battery", enforce_policy=False, role=role)

    resp = TestClient(real_app).post(
        "/login",
        data={"username": "someone", "password": "Correct-Horse-9-battery"},
        headers=_ORIGIN,
        follow_redirects=False,
    )

    assert (resp.status_code, resp.headers["location"]) == (302, landing)


def test_a_viewer_lands_on_watch_after_a_forced_password_change(real_app, conn):
    create_user(
        conn,
        "v",
        "Correct-Horse-9-battery",
        enforce_policy=False,
        role="viewer",
        must_change_password=True,
    )
    client = TestClient(real_app)
    client.cookies.set("session_token", create_session(conn, "v"))

    resp = client.post(
        "/force-password-change",
        data={
            "old_password": "Correct-Horse-9-battery",
            "new_password": "Staple-Lantern-42-orbit",
            "confirm_password": "Staple-Lantern-42-orbit",
        },
        headers=_ORIGIN,
        follow_redirects=False,
    )

    assert (resp.status_code, resp.headers["location"]) == (302, "/watch")


def test_a_viewer_can_reconfirm_their_password(guarded_app, conn, viewer_client):
    app, reached = guarded_app
    client = viewer_client(app, conn)

    resp = client.post("/api/auth/reauth", json={"password": "x"}, headers=_ORIGIN)

    assert resp.status_code == 204
    assert [(m, p) for m, p, _ in reached] == [("POST", "/api/auth/reauth")]
