"""Tests for the kept/protected media API routes."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.routes.kept import router as kept_router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(kept_router)
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


def _insert_media_item(conn, media_id: str, title: str = "Test Movie", media_type: str = "movie") -> None:
    conn.execute(
        "INSERT INTO media_items (id, title, media_type, plex_library_id, plex_rating_key, "
        "added_at, file_path, file_size_bytes) VALUES (?, ?, ?, 1, 'rk1', ?, '/f', 0)",
        (media_id, title, media_type, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _insert_protection(conn, media_item_id: str, action: str = "protected_forever") -> None:
    conn.execute(
        "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, token, token_used) "
        "VALUES (?, ?, ?, ?, 0)",
        (media_item_id, action, datetime.now(timezone.utc).isoformat(), f"tok-{media_item_id}"),
    )
    conn.commit()


class TestApiKept:
    def test_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/kept")
        assert resp.status_code == 401

    def test_returns_empty(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/kept")
        assert resp.status_code == 200
        body = resp.json()
        assert "forever" in body
        assert "snoozed" in body
        assert body["forever"] == []
        assert body["snoozed"] == []

    def test_returns_protected_items(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        _insert_media_item(conn, "m1", "Inception")
        _insert_protection(conn, "m1", "protected_forever")
        resp = client.get("/api/kept")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["forever"]) == 1
        assert body["forever"][0]["title"] == "Inception"


class TestApiUnprotect:
    def test_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/media/m1/unprotect")
        assert resp.status_code == 401

    def test_unprotect_not_found_returns_404(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.post("/api/media/m1/unprotect")
        assert resp.status_code == 404
        assert "No active protection found" in resp.json()["error"]

    def test_unprotect_happy_path(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        _insert_media_item(conn, "m1", "Dune")
        _insert_protection(conn, "m1", "protected_forever")
        resp = client.post("/api/media/m1/unprotect")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        remaining = conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE media_item_id='m1' "
            "AND action='protected_forever'"
        ).fetchone()[0]
        assert remaining == 0
        audit = conn.execute(
            "SELECT action FROM audit_log WHERE media_item_id='m1'"
        ).fetchone()
        assert audit is not None
        assert audit["action"] == "unprotected"


class TestApiShowSeasons:
    def test_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/show/rk_show/seasons")
        assert resp.status_code == 401

    def test_empty_returns_no_seasons(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/show/rk_nonexistent/seasons")
        assert resp.status_code == 200
        body = resp.json()
        assert body["seasons"] == []
        assert body["show_title"] == ""
