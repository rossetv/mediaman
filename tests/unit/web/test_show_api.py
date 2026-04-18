"""Tests for show-level keep API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.main import create_app


@pytest.fixture
def app(db_path, secret_key):
    conn = init_db(str(db_path))
    set_connection(conn)
    application = create_app()
    application.state.config = MagicMock(secret_key=secret_key, data_dir=str(db_path.parent))
    application.state.db = conn
    yield application
    conn.close()


@pytest.fixture
def authed_client(app):
    from mediaman.auth.session import create_session, create_user
    conn = app.state.db
    create_user(conn, "testadmin", "testpass123")
    token = create_session(conn, "testadmin")
    client = TestClient(app)
    client.cookies.set("session_token", token)
    return client


def _insert_season(conn, item_id, show_title, season_num, show_rating_key):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO media_items (id, title, media_type, show_title, season_number, "
        "plex_library_id, plex_rating_key, show_rating_key, added_at, file_path, file_size_bytes) "
        "VALUES (?, ?, 'tv_season', ?, ?, 2, ?, ?, ?, '/media/tv/test', 5000000000)",
        (item_id, show_title, show_title, season_num, item_id, show_rating_key, now),
    )
    conn.commit()


class TestShowSeasonsAPI:
    def test_returns_seasons_for_show(self, authed_client, app):
        conn = app.state.db
        _insert_season(conn, "101", "Breaking Bad", 1, "100")
        _insert_season(conn, "102", "Breaking Bad", 2, "100")
        resp = authed_client.get("/api/show/100/seasons")
        assert resp.status_code == 200
        data = resp.json()
        assert data["show_title"] == "Breaking Bad"
        assert len(data["seasons"]) == 2

    def test_returns_empty_for_unknown_show(self, authed_client):
        resp = authed_client.get("/api/show/999/seasons")
        assert resp.status_code == 200
        assert resp.json()["seasons"] == []

    def test_requires_auth(self, app):
        client = TestClient(app)
        resp = client.get("/api/show/100/seasons")
        assert resp.status_code == 401


class TestShowKeepAPI:
    def test_keeps_show_forever(self, authed_client, app):
        conn = app.state.db
        _insert_season(conn, "101", "Breaking Bad", 1, "100")
        _insert_season(conn, "102", "Breaking Bad", 2, "100")
        resp = authed_client.post("/api/show/100/keep", json={
            "duration": "forever",
            "season_ids": ["101", "102"],
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='100'").fetchone()
        assert row is not None
        assert row["action"] == "protected_forever"
        actions = conn.execute(
            "SELECT * FROM scheduled_actions WHERE action='protected_forever'"
        ).fetchall()
        assert len(actions) == 2

    def test_keeps_show_timed(self, authed_client, app):
        conn = app.state.db
        _insert_season(conn, "101", "Breaking Bad", 1, "100")
        resp = authed_client.post("/api/show/100/keep", json={
            "duration": "30 days",
            "season_ids": ["101"],
        })
        assert resp.status_code == 200
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='100'").fetchone()
        assert row["action"] == "snoozed"
        assert row["execute_at"] is not None

    def test_rejects_empty_season_ids(self, authed_client):
        resp = authed_client.post("/api/show/100/keep", json={
            "duration": "forever",
            "season_ids": [],
        })
        assert resp.status_code == 400


class TestShowRemoveAPI:
    def test_removes_show_keep(self, authed_client, app):
        conn = app.state.db
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO kept_shows (show_rating_key, show_title, action, created_at) "
            "VALUES ('100', 'Breaking Bad', 'protected_forever', ?)", (now,),
        )
        conn.commit()
        resp = authed_client.post("/api/show/100/remove")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='100'").fetchone()
        assert row is None

    def test_returns_404_for_unknown(self, authed_client):
        resp = authed_client.post("/api/show/999/remove")
        assert resp.status_code == 404
