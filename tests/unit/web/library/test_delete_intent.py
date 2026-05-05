"""Tests for delete-intent persistence and reconciliation (finding 24)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.auth.session import create_session, create_user
from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.library_api import (
    _DELETE_LIMITER,
    _complete_delete_intent,
    _record_delete_intent,
    reconcile_pending_delete_intents,
)
from mediaman.web.routes.library_api import router as library_api_router


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(library_router)
    app.include_router(library_api_router)
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


def _insert_movie(conn, media_id: str = "m1", radarr_id: int | None = 101) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO media_items "
        "(id, title, media_type, plex_library_id, plex_rating_key, added_at, "
        "file_path, file_size_bytes, radarr_id) "
        "VALUES (?, 'Test Movie', 'movie', 1, 'rk1', ?, '/media/movie.mkv', 1000000, ?)",
        (media_id, now, radarr_id),
    )
    conn.commit()


class TestRecordDeleteIntent:
    """Unit tests for the delete-intent DB helpers."""

    def test_record_creates_pending_row(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        intent_id = _record_delete_intent(conn, "m1", "radarr", 101)
        row = conn.execute("SELECT * FROM delete_intents WHERE id = ?", (intent_id,)).fetchone()
        assert row is not None
        assert row["media_item_id"] == "m1"
        assert row["target_kind"] == "radarr"
        assert row["target_id"] == "101"
        assert row["completed_at"] is None
        assert row["started_at"] is not None

    def test_complete_sets_completed_at(self, db_path):
        conn = init_db(str(db_path))
        set_connection(conn)
        intent_id = _record_delete_intent(conn, "m1", "radarr", 101)
        _complete_delete_intent(conn, intent_id)
        row = conn.execute(
            "SELECT completed_at FROM delete_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        assert row["completed_at"] is not None


class TestDeleteIntentPersistence:
    """Integration: delete endpoint must write intent before external call."""

    def setup_method(self):
        _DELETE_LIMITER.reset()

    def test_intent_created_before_radarr_call(self, db_path, secret_key):
        """An intent row is written before the Radarr call; verified by checking DB after delete."""
        conn = init_db(str(db_path))
        _insert_movie(conn)
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        mock_radarr = MagicMock()

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        mock_radarr.delete_movie.assert_called_once()

        # After a successful delete an intent row should exist and be completed.
        all_intents = conn.execute(
            "SELECT id, media_item_id, completed_at FROM delete_intents"
        ).fetchall()
        assert len(all_intents) >= 1
        assert all(row["completed_at"] is not None for row in all_intents)

    def test_intent_completed_after_successful_delete(self, db_path, secret_key):
        """After a successful delete the intent row is marked completed."""
        conn = init_db(str(db_path))
        _insert_movie(conn)
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        mock_radarr = MagicMock()
        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        pending = conn.execute(
            "SELECT id FROM delete_intents WHERE completed_at IS NULL"
        ).fetchall()
        assert len(pending) == 0, "No intent rows should remain pending after a clean delete"

    def test_intent_remains_pending_when_radarr_fails(self, db_path, secret_key):
        """When the Radarr call fails the intent stays unresolved."""
        conn = init_db(str(db_path))
        _insert_movie(conn)
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.delete_movie.side_effect = RuntimeError("Radarr down")

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 502
        pending = conn.execute(
            "SELECT id FROM delete_intents WHERE completed_at IS NULL"
        ).fetchall()
        assert len(pending) == 1, "Intent must stay pending on failure"


class TestReconcilePendingDeleteIntents:
    """Unit tests for the reconcile helper (finding 24)."""

    def setup_method(self):
        _DELETE_LIMITER.reset()

    def test_reconcile_clears_intent_when_item_already_gone(self, db_path):
        """If the media row is already gone the intent is just completed."""
        conn = init_db(str(db_path))
        set_connection(conn)
        # Insert an intent without a corresponding media_items row.
        intent_id = _record_delete_intent(conn, "ghost-m1", "radarr", 99)
        resolved = reconcile_pending_delete_intents()
        assert resolved >= 1
        row = conn.execute(
            "SELECT completed_at FROM delete_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        assert row["completed_at"] is not None

    def test_reconcile_removes_orphaned_media_row(self, db_path):
        """An unresolved intent for an existing media row is cleaned up."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _insert_movie(conn, "m2", radarr_id=None)
        _record_delete_intent(conn, "m2", "radarr", "none")

        resolved = reconcile_pending_delete_intents()
        assert resolved >= 1

        # Media row must be gone.
        row = conn.execute("SELECT id FROM media_items WHERE id = 'm2'").fetchone()
        assert row is None

    def test_reconcile_is_idempotent(self, db_path):
        """Running reconcile twice is safe; second run resolves nothing new."""
        conn = init_db(str(db_path))
        set_connection(conn)
        _record_delete_intent(conn, "gone-m3", "radarr", 42)

        first = reconcile_pending_delete_intents()
        second = reconcile_pending_delete_intents()

        # Second run should find nothing to do.
        assert second == 0
        assert first >= 1
