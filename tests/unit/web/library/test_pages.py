"""Tests for :mod:`mediaman.web.routes.library.pages`.

GET /library page route:
  - unauthenticated → redirect to /login
  - authenticated → 200 with rendered HTML context
  - sort/type/page params are clamped and sanitised
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.library.pages import router as pages_router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(pages_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)

    mock_templates = MagicMock()

    def fake_template_response(request, template_name, ctx):
        return HTMLResponse(json.dumps(ctx, default=str), status_code=200)

    mock_templates.TemplateResponse.side_effect = fake_template_response
    app.state.templates = mock_templates
    return app


def _auth_client(app: FastAPI, conn) -> TestClient:
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _insert_movie(conn, media_id: str, title: str = "Test Movie") -> None:
    conn.execute(
        "INSERT INTO media_items (id, title, media_type, plex_library_id, plex_rating_key, "
        "added_at, file_path, file_size_bytes) VALUES (?, ?, 'movie', 1, ?, ?, '/f', 1000000)",
        (media_id, title, f"rk-{media_id}", _now_iso()),
    )
    conn.commit()


class TestLibraryPage:
    def test_unauthenticated_redirects_to_login(self, db_path, secret_key):
        """GET /library without a valid session redirects to /login."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/library", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/login")

    def test_authenticated_returns_200(self, db_path, secret_key):
        """GET /library with a valid session returns 200."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/library")
        assert resp.status_code == 200

    def test_context_includes_username(self, db_path, secret_key):
        """Rendered context must include the admin username."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/library")
        ctx = resp.json()
        assert ctx["username"] == "admin"

    def test_context_includes_items_and_stats(self, db_path, secret_key):
        """Rendered context includes items list and stats dict."""
        conn = init_db(str(db_path))
        _insert_movie(conn, "m1", "Dune")
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/library")
        ctx = resp.json()
        assert "items" in ctx
        assert "stats" in ctx
        assert len(ctx["items"]) == 1

    def test_invalid_sort_is_sanitised_to_default(self, db_path, secret_key):
        """An unknown sort value is silently clamped to added_desc."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/library?sort=invalid_sort")
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["current_sort"] == "added_desc"

    def test_invalid_type_is_sanitised_to_empty(self, db_path, secret_key):
        """An unknown type value is sanitised to an empty string (show all)."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/library?type=invalid_type")
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["current_type"] == ""

    def test_page_below_minimum_rejected(self, db_path, secret_key):
        """A page value of 0 is rejected with 422 (finding 17).

        Pagination bounds are now enforced at the input layer — invalid
        values surface as a Pydantic validation error rather than being
        silently rewritten to 1.
        """
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/library?page=0")
        assert resp.status_code == 422

    def test_per_page_above_maximum_rejected(self, db_path, secret_key):
        """A per_page value above 100 is rejected with 422 (finding 17)."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/library?per_page=9999")
        assert resp.status_code == 422

    def test_pagination_metadata_correct(self, db_path, secret_key):
        """page_start, page_end, and total_pages are computed correctly."""
        conn = init_db(str(db_path))
        for i in range(5):
            _insert_movie(conn, f"m{i}", f"Film {i}")
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/library?per_page=2&page=1")
        ctx = resp.json()
        assert ctx["total"] == 5
        assert ctx["total_pages"] == 3
        assert ctx["page_start"] == 1
        assert ctx["page_end"] == 2

    def test_search_query_propagated_to_context(self, db_path, secret_key):
        """The search query string is echoed back in the template context."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/library?q=Dune")
        ctx = resp.json()
        assert ctx["q"] == "Dune"
