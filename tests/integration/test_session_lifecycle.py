"""Integration: login → use session → logout → session destroyed.

Exercises the full auth seam through real HTTP routes + real DB:
  POST /login  →  session created in DB  →  cookie returned
  GET /api/downloads  →  session validated against DB
  POST /api/auth/logout  →  session destroyed in DB  →  cookie cleared
  GET /api/downloads (after logout)  →  401

No internal mocking — the only fake is MEDIAMAN_FORCE_SECURE_COOKIES=false
so TestClient (HTTP) can set cookies normally.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.auth.session import create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.auth import router as auth_router
from mediaman.web.routes.downloads import router as downloads_router

_TPL_DIR = Path(__file__).parent.parent.parent / "src" / "mediaman" / "web" / "templates"


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(downloads_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    app.state.templates = Jinja2Templates(directory=str(_TPL_DIR))
    set_connection(conn)
    return app


class TestSessionLifecycle:
    def test_login_use_logout_cycle(self, db_path, secret_key, monkeypatch):
        """Full session lifecycle: login, authenticated call, logout, then 401."""
        monkeypatch.setenv("MEDIAMAN_FORCE_SECURE_COOKIES", "false")

        conn = init_db(str(db_path))
        create_user(conn, "admin", "P@ssw0rd!Str0ng", enforce_policy=False)

        app = _make_app(conn, secret_key)
        # follow_redirects=True so login POST redirects us transparently.
        client = TestClient(app, raise_server_exceptions=True, follow_redirects=False)

        # 1. Login → expect redirect to / and a session cookie.
        resp = client.post("/login", data={"username": "admin", "password": "P@ssw0rd!Str0ng"})
        assert resp.status_code in (302, 303)
        session_cookie = client.cookies.get("session_token")
        assert session_cookie, "session cookie must be set after successful login"

        # Verify the session row exists in the DB.
        from mediaman.auth.session_store import _hash_token

        token_hash = _hash_token(session_cookie)
        row = conn.execute(
            "SELECT username FROM admin_sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()
        assert row is not None
        assert row["username"] == "admin"

        # 2. Authenticated request — downloads API must return 200.
        resp = client.get("/api/downloads")
        assert resp.status_code == 200

        # 3. Logout → redirect to /login, cookie cleared.
        resp = client.post("/api/auth/logout")
        assert resp.status_code in (302, 303)
        # The Set-Cookie header must clear the session_token.
        set_cookie_header = resp.headers.get("set-cookie", "")
        assert "session_token" in set_cookie_header

        # Session row must be gone.
        row_after = conn.execute(
            "SELECT username FROM admin_sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()
        assert row_after is None

        # 4. Subsequent API call must be rejected now the session is gone.
        resp = client.get("/api/downloads")
        assert resp.status_code == 401

    def test_invalid_session_is_rejected(self, db_path, secret_key):
        """A fabricated / unknown session token returns 401."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", "this-is-not-a-valid-token")

        resp = client.get("/api/downloads")
        assert resp.status_code == 401

    def test_logout_without_session_returns_401(self, db_path, secret_key):
        """Logout with no active session cookie returns 401 — not a CSRF-able reset."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post("/api/auth/logout")
        assert resp.status_code == 401
