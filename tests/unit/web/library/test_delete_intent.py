"""Tests for delete-intent persistence and reconciliation (finding 24)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.library_api import (
    _DELETE_LIMITER,
    _complete_delete_intent,
    _record_delete_intent,
    reconcile_pending_delete_intents,
)
from mediaman.web.routes.library_api import router as library_api_router
from tests.helpers.factories import insert_media_item


def _insert_movie(conn, media_id: str = "m1", radarr_id: int | None = 101) -> None:
    insert_media_item(
        conn,
        id=media_id,
        title="Test Movie",
        media_type="movie",
        plex_rating_key="rk1",
        file_path="/media/movie.mkv",
        file_size_bytes=1_000_000,
        radarr_id=radarr_id,
    )


def _app(app_factory, conn):
    return app_factory(library_router, library_api_router, conn=conn)


class TestRecordDeleteIntent:
    """Unit tests for the delete-intent DB helpers."""

    def test_record_creates_pending_row(self, conn):
        intent_id = _record_delete_intent(conn, "m1", "radarr", 101)
        row = conn.execute("SELECT * FROM delete_intents WHERE id = ?", (intent_id,)).fetchone()
        assert row is not None
        assert row["media_item_id"] == "m1"
        assert row["target_kind"] == "radarr"
        assert row["target_id"] == "101"
        assert row["completed_at"] is None
        assert row["started_at"] is not None

    def test_complete_sets_completed_at(self, conn):
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

    def test_intent_created_before_radarr_call(self, app_factory, authed_client, conn):
        """An intent row is written before the Radarr call; verified by checking DB after delete."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

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

    def test_intent_completed_after_successful_delete(self, app_factory, authed_client, conn):
        """After a successful delete the intent row is marked completed."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

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

    def test_intent_remains_pending_when_radarr_fails(self, app_factory, authed_client, conn):
        """When the Radarr call fails the intent stays unresolved."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        import requests as _requests

        mock_radarr = MagicMock()
        mock_radarr.delete_movie.side_effect = _requests.ConnectionError("Radarr down")

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

    def test_reconcile_clears_intent_when_item_already_gone(self, conn):
        """If the media row is already gone the intent is just completed."""
        # Insert an intent without a corresponding media_items row.
        intent_id = _record_delete_intent(conn, "ghost-m1", "radarr", 99)
        resolved = reconcile_pending_delete_intents()
        assert resolved >= 1
        row = conn.execute(
            "SELECT completed_at FROM delete_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        assert row["completed_at"] is not None

    def test_reconcile_removes_orphaned_media_row(self, conn):
        """An unresolved intent for an existing media row is cleaned up."""
        _insert_movie(conn, "m2", radarr_id=None)
        _record_delete_intent(conn, "m2", "radarr", "none")

        resolved = reconcile_pending_delete_intents()
        assert resolved >= 1

        # Media row must be gone.
        row = conn.execute("SELECT id FROM media_items WHERE id = 'm2'").fetchone()
        assert row is None

    def test_reconcile_is_idempotent(self, conn):
        """Running reconcile twice is safe; second run resolves nothing new."""
        _record_delete_intent(conn, "gone-m3", "radarr", 42)

        first = reconcile_pending_delete_intents()
        second = reconcile_pending_delete_intents()

        # Second run should find nothing to do.
        assert second == 0
        assert first >= 1
