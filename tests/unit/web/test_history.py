"""Tests for the history API — paginated audit log with action-type filter."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.history import _PER_PAGE_DEFAULT, _PER_PAGE_MAX
from mediaman.web.routes.history import router as history_router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(history_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


def _auth_client(app: FastAPI, conn) -> TestClient:
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


def _insert_audit_row(conn, action: str = "scanned", media_item_id: str = "m1") -> None:
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, created_at) VALUES (?, ?, ?)",
        (media_item_id, action, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


class TestApiHistory:
    def test_history_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/history")
        assert resp.status_code == 401

    def test_history_empty_returns_valid_shape(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []
        assert body["page"] == 1
        assert "per_page" in body
        assert "total_pages" in body

    def test_history_returns_rows(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        for action in ("scanned", "deleted", "kept"):
            _insert_audit_row(conn, action=action)
        resp = client.get("/api/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 3
        item = body["items"][0]
        assert "action" in item
        assert "created_at" in item

    def test_history_action_filter(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        _insert_audit_row(conn, action="deleted", media_item_id="m1")
        _insert_audit_row(conn, action="deleted", media_item_id="m2")
        _insert_audit_row(conn, action="scanned", media_item_id="m3")
        resp = client.get("/api/history?action=deleted")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert all(i["action"] == "deleted" for i in body["items"])

    def test_history_invalid_action_filter_ignored(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        _insert_audit_row(conn, action="scanned", media_item_id="m1")
        _insert_audit_row(conn, action="deleted", media_item_id="m2")
        resp = client.get("/api/history?action=bogus_action_xyz")
        assert resp.status_code == 200
        # Invalid filter is silently dropped — all rows returned
        assert resp.json()["total"] == 2

    def test_history_pagination(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        for i in range(5):
            _insert_audit_row(conn, media_item_id=f"m{i}")

        resp = client.get("/api/history?per_page=2&page=1")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2
        assert resp.json()["total_pages"] == 3

        resp = client.get("/api/history?per_page=2&page=3")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

    def test_per_page_max_enforced(self, db_path, secret_key):
        """per_page above the maximum must be clamped/rejected by the Query constraint."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.get(f"/api/history?per_page={_PER_PAGE_MAX + 1}")
        # FastAPI Query(le=...) returns 422 Unprocessable Entity for out-of-range values.
        assert resp.status_code == 422

    def test_per_page_zero_rejected(self, db_path, secret_key):
        """per_page=0 must be rejected."""
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/history?per_page=0")
        assert resp.status_code == 422

    def test_shared_per_page_constants(self):
        """_PER_PAGE_DEFAULT and _PER_PAGE_MAX are within sensible ranges."""
        assert 1 <= _PER_PAGE_DEFAULT <= _PER_PAGE_MAX
        assert _PER_PAGE_MAX <= 100
