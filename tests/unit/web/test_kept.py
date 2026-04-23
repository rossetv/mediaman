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


def _insert_season(conn, media_id: str, show_rating_key: str | None, show_title: str, season: int = 1) -> None:
    """Insert a TV season with a specific show_rating_key / show_title."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO media_items
           (id, title, media_type, plex_library_id, plex_rating_key, added_at,
            file_path, file_size_bytes, show_rating_key, show_title, season_number)
           VALUES (?, ?, 'tv_season', 1, ?, ?, '/p', 1, ?, ?, ?)""",
        (media_id, f"{show_title} S{season}", f"rk-{media_id}", now, show_rating_key, show_title, season),
    )
    conn.commit()


class TestKeepShowIdorDefence:
    """C13 — /api/show/{key}/keep must not collide two shows sharing a title."""

    def test_seasons_owned_by_different_show_rejected(self, db_path, secret_key):
        """A season_id from a different show with the same title is refused."""
        conn = init_db(str(db_path))
        # Two distinct shows, both titled "Kingdom", different rating keys.
        _insert_season(conn, "m-A", "rk-show-A", "Kingdom", season=1)
        _insert_season(conn, "m-B", "rk-show-B", "Kingdom", season=1)

        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        # Try to keep show A but pass show B's season id — must be rejected.
        resp = client.post(
            "/api/show/rk-show-A/keep",
            json={"duration": "forever", "season_ids": ["m-B"]},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False
        # No protection row was created for m-B
        row = conn.execute(
            "SELECT 1 FROM scheduled_actions WHERE media_item_id='m-B'"
        ).fetchone()
        assert row is None

    def test_unknown_rating_key_returns_409(self, db_path, secret_key):
        """A rating_key with no matching media_items row is refused with 409."""
        conn = init_db(str(db_path))
        _insert_season(conn, "m-A", "rk-show-A", "Kingdom", season=1)

        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.post(
            "/api/show/rk-does-not-exist/keep",
            json={"duration": "forever", "season_ids": ["m-A"]},
        )
        assert resp.status_code == 409
        assert resp.json()["ok"] is False

    def test_correct_ownership_still_allowed(self, db_path, secret_key):
        """Happy path — seasons with matching show_rating_key are accepted."""
        conn = init_db(str(db_path))
        _insert_season(conn, "m-A1", "rk-show-A", "Kingdom", season=1)
        _insert_season(conn, "m-A2", "rk-show-A", "Kingdom", season=2)

        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        resp = client.post(
            "/api/show/rk-show-A/keep",
            json={"duration": "forever", "season_ids": ["m-A1", "m-A2"]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_actions WHERE media_item_id IN ('m-A1','m-A2')"
        ).fetchone()
        assert rows["n"] == 2


class TestResolveShowRatingKey:
    """C13 helper unit tests."""

    def test_empty_supplied_key_is_refused(self, db_path, secret_key):
        from mediaman.web.routes.kept import _resolve_show_rating_key
        conn = init_db(str(db_path))
        resolved, err = _resolve_show_rating_key(conn, "")
        assert resolved is None
        assert err is not None

    def test_known_key_resolves(self, db_path, secret_key):
        from mediaman.web.routes.kept import _resolve_show_rating_key
        conn = init_db(str(db_path))
        _insert_season(conn, "m-A", "rk-known", "Show", season=1)
        resolved, err = _resolve_show_rating_key(conn, "rk-known")
        assert resolved == "rk-known"
        assert err is None

    def test_unknown_key_returns_error(self, db_path, secret_key):
        from mediaman.web.routes.kept import _resolve_show_rating_key
        conn = init_db(str(db_path))
        resolved, err = _resolve_show_rating_key(conn, "rk-unknown")
        assert resolved is None
        assert err is not None
