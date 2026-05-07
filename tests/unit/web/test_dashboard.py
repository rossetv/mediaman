"""Tests for dashboard JSON API endpoints (stats, scheduled, deleted, reclaimed-chart)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.dashboard import router as dashboard_router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)
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


def _insert_media_item(
    conn,
    media_id: str,
    title: str,
    media_type: str = "movie",
    file_size: int = 1_000_000,
    plex_rating_key: str = "rk1",
) -> None:
    conn.execute(
        "INSERT INTO media_items "
        "(id, title, media_type, plex_library_id, plex_rating_key, added_at, file_path, file_size_bytes) "
        "VALUES (?, ?, ?, 1, ?, ?, '/media/f.mkv', ?)",
        (
            media_id,
            title,
            media_type,
            plex_rating_key,
            datetime.now(UTC).isoformat(),
            file_size,
        ),
    )
    conn.commit()


def _insert_scheduled_deletion(conn, media_item_id: str, execute_at: str | None = None) -> None:
    if execute_at is None:
        execute_at = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    conn.execute(
        "INSERT INTO scheduled_actions "
        "(media_item_id, action, scheduled_at, execute_at, token, token_used) "
        "VALUES (?, 'scheduled_deletion', ?, ?, ?, 0)",
        (
            media_item_id,
            datetime.now(UTC).isoformat(),
            execute_at,
            f"tok-{media_item_id}",
        ),
    )
    conn.commit()


def _insert_audit_deleted(conn, media_item_id: str, space_bytes: int = 500_000_000) -> None:
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, space_reclaimed_bytes, created_at) "
        "VALUES (?, 'deleted', ?, ?)",
        (media_item_id, space_bytes, datetime.now(UTC).isoformat()),
    )
    conn.commit()


class TestApiDashboardStats:
    def test_stats_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/dashboard/stats")
        assert resp.status_code == 401

    def test_stats_returns_shape(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/dashboard/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "storage" in body
        assert "reclaimed_total_bytes" in body
        assert "reclaimed_total" in body
        assert body["reclaimed_total_bytes"] == 0

    def test_stats_accumulates_reclaimed(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        _insert_media_item(conn, "m1", "Dune")
        _insert_audit_deleted(conn, "m1", space_bytes=500_000_000)
        resp = client.get("/api/dashboard/stats")
        assert resp.json()["reclaimed_total_bytes"] == 500_000_000


class TestApiDashboardScheduled:
    def test_scheduled_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/dashboard/scheduled")
        assert resp.status_code == 401

    def test_scheduled_empty(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/dashboard/scheduled")
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    def test_scheduled_returns_items(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        _insert_media_item(conn, "m1", "Dune", plex_rating_key="rk42")
        _insert_scheduled_deletion(conn, "m1")
        resp = client.get("/api/dashboard/scheduled")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["title"] == "Dune"
        assert "countdown" in items[0]
        assert "file_size" in items[0]


class TestApiDashboardDeleted:
    def test_deleted_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/dashboard/deleted")
        assert resp.status_code == 401

    def test_deleted_empty(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/dashboard/deleted")
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    def test_deleted_returns_items(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        _insert_media_item(conn, "m1", "Interstellar", plex_rating_key="rk99")
        _insert_audit_deleted(conn, "m1")
        resp = client.get("/api/dashboard/deleted")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["title"] == "Interstellar"
        assert "reclaimed" in items[0]
        assert "deleted_ago" in items[0]


class TestApiDashboardReclaimedChart:
    def test_chart_requires_auth(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/dashboard/reclaimed-chart")
        assert resp.status_code == 401

    def test_chart_empty(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/dashboard/reclaimed-chart")
        assert resp.status_code == 200
        assert resp.json() == {"weeks": []}

    def test_chart_aggregates_by_week(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        # Two deletions in the same week
        now = datetime.now(UTC)
        for space in (100, 200):
            conn.execute(
                "INSERT INTO audit_log (media_item_id, action, space_reclaimed_bytes, created_at) "
                "VALUES (?, 'deleted', ?, ?)",
                (f"m-{space}", space, now.isoformat()),
            )
        conn.commit()
        resp = client.get("/api/dashboard/reclaimed-chart")
        assert resp.status_code == 200
        weeks = resp.json()["weeks"]
        assert len(weeks) == 1
        assert weeks[0]["reclaimed_bytes"] == 300
        assert re.match(r"\d{4}-W\d{2}", weeks[0]["week"])
        assert isinstance(weeks[0]["reclaimed"], str)
        assert len(weeks[0]["reclaimed"]) > 0
