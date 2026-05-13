"""Admin-email lookup and the no-email skip path for download submit."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.db.schema_definition import _SCHEMA
from mediaman.main import create_app
from mediaman.web.auth.password_hash import create_user, set_user_email
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.search import (
    _DOWNLOAD_ADMIN_LIMITER,
    _DOWNLOAD_IP_LIMITER,
    _download_dedup,
)
from mediaman.web.routes.search.download import _resolve_admin_email

# ---------------------------------------------------------------------------
# Unit tests for _resolve_admin_email
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    create_user(c, "rossetv", "TestPass!12345", enforce_policy=False)
    return c


def test_resolve_admin_email_returns_none_when_unset(conn: sqlite3.Connection) -> None:
    assert _resolve_admin_email(conn, "rossetv") is None


def test_resolve_admin_email_returns_address_when_set(conn: sqlite3.Connection) -> None:
    set_user_email(conn, "rossetv", "admin@example.com")
    assert _resolve_admin_email(conn, "rossetv") == "admin@example.com"


def test_resolve_admin_email_returns_none_for_unknown_admin(conn: sqlite3.Connection) -> None:
    assert _resolve_admin_email(conn, "ghost") is None


# ---------------------------------------------------------------------------
# Integration-style tests: submit movie via API, check notification rows
# ---------------------------------------------------------------------------


@pytest.fixture
def _app_with_db(db_path, secret_key):
    """Full app wired to a temp DB, yielding (app, db_conn)."""
    db_conn = init_db(str(db_path))
    set_connection(db_conn)
    db_conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
        "VALUES ('tmdb_read_token', 'test-token', 0, datetime('now'))"
    )
    db_conn.commit()
    application = create_app()
    application.state.config = MagicMock(secret_key=secret_key, data_dir=str(db_path.parent))
    application.state.db = db_conn
    yield application, db_conn
    db_conn.close()


@pytest.fixture(autouse=True)
def _reset_limiters():
    _DOWNLOAD_ADMIN_LIMITER.reset()
    _DOWNLOAD_IP_LIMITER.reset()
    _download_dedup.clear()


def _authed_client(application, db_conn, username: str = "admin") -> TestClient:
    token = create_session(db_conn, username)
    client = TestClient(application)
    client.cookies.set("session_token", token)
    client.headers.update({"Origin": "http://testserver"})
    return client


_MOVIE_BODY = {"media_type": "movie", "tmdb_id": 12345, "title": "Inception"}


def test_submit_movie_skips_notification_when_admin_has_no_email(_app_with_db):
    """When the admin has no email set, submitting a movie returns 200 but no notification row."""
    application, db_conn = _app_with_db
    create_user(db_conn, "admin", "password1234", enforce_policy=False)
    # Deliberately do NOT call set_user_email — admin email is NULL.

    mock_radarr = MagicMock()
    mock_radarr.get_movie_by_tmdb.return_value = None
    mock_radarr.add_movie.return_value = None

    with patch(
        "mediaman.web.routes.search.download.build_radarr_from_db", return_value=mock_radarr
    ):
        resp = _authed_client(application, db_conn).post("/api/search/download", json=_MOVIE_BODY)

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    count = db_conn.execute("SELECT COUNT(*) FROM download_notifications").fetchone()[0]
    assert count == 0


def test_submit_movie_records_notification_when_admin_has_email(_app_with_db):
    """When the admin has a real email set, submitting a movie inserts a notification row."""
    application, db_conn = _app_with_db
    create_user(db_conn, "admin", "password1234", enforce_policy=False)
    set_user_email(db_conn, "admin", "admin@example.com")

    mock_radarr = MagicMock()
    mock_radarr.get_movie_by_tmdb.return_value = None
    mock_radarr.add_movie.return_value = None

    with patch(
        "mediaman.web.routes.search.download.build_radarr_from_db", return_value=mock_radarr
    ):
        resp = _authed_client(application, db_conn).post("/api/search/download", json=_MOVIE_BODY)

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    row = db_conn.execute("SELECT email FROM download_notifications WHERE tmdb_id=12345").fetchone()
    assert row is not None
    assert row[0] == "admin@example.com"
