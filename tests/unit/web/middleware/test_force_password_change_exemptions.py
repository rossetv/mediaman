"""A flagged admin is funnelled away from pages, but not from the token-authorised unsubscribe."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.web.auth.password_hash import create_user, set_must_change_password
from mediaman.web.auth.session_store import create_session
from mediaman.web.middleware.force_password_change import ForcePasswordChangeMiddleware


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "off")
    conn = init_db(str(db_path))
    create_user(conn, "alice", "correct-password-99", enforce_policy=False)
    set_must_change_password(conn, "alice", True)
    set_connection(conn)
    app = FastAPI()
    app.add_middleware(ForcePasswordChangeMiddleware)

    @app.get("/unsubscribe")
    @app.post("/unsubscribe")
    @app.get("/unsubscribe-other")
    @app.get("/library")
    def _page():
        return {"ok": True}

    c = TestClient(app)
    c.cookies.set("session_token", create_session(conn, "alice"))
    return c


def test_unsubscribe_is_not_redirected_for_a_flagged_user(client):
    assert client.get("/unsubscribe?token=t", follow_redirects=False).status_code == 200
    assert client.post("/unsubscribe", data={"token": "t"}).status_code == 200


def test_other_pages_still_redirect_a_flagged_user(client):
    resp = client.get("/library", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/force-password-change"
    # only the exact unsubscribe path is exempt, not anything that starts with it
    assert client.get("/unsubscribe-other", follow_redirects=False).status_code == 302
