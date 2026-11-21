from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.core.scheduled_action_kinds import ACTION_PROTECTED_FOREVER, ACTION_SCHEDULED_DELETION
from mediaman.services.infra import SafeHTTPError
from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.library_api import (
    _DELETE_LIMITER,
    _complete_delete_intent,
    _record_delete_intent,
    reconcile_pending_delete_intents,
)
from mediaman.web.routes.library_api import router as library_api_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action


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


def _insert_tv_season(conn, media_id: str = "s1") -> None:
    insert_media_item(
        conn,
        id=media_id,
        title="The Show",
        media_type="tv_season",
        plex_rating_key="rk2",
        file_path="/media/show",
        file_size_bytes=2_000_000,
        sonarr_id=202,
        season_number=1,
        show_title="The Show",
        show_rating_key="show-rk",
    )


def _app(app_factory, conn):
    return app_factory(library_router, library_api_router, conn=conn)


class TestRecordDeleteIntent:
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

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
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

    def test_intent_closed_with_error_when_radarr_fails(self, app_factory, authed_client, conn):
        """When the Radarr call fails the intent is closed with its error recorded."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        from mediaman.services.infra import SafeHTTPError

        mock_radarr = MagicMock()
        mock_radarr.delete_movie.side_effect = SafeHTTPError(
            status_code=0, body_snippet="transport error: ConnectionError", url="http://radarr/"
        )

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 502
        rows = conn.execute("SELECT completed_at, last_error FROM delete_intents").fetchall()
        assert len(rows) == 1
        assert rows[0]["completed_at"] is not None
        assert rows[0]["last_error"]


class TestReconcilePendingDeleteIntents:
    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        _DELETE_LIMITER.reset()

    def test_reconcile_clears_intent_when_item_already_gone(self, conn):
        """If the media row is already gone the intent is just completed."""
        # Insert an intent without a corresponding media_items row.
        intent_id = _record_delete_intent(conn, "ghost-m1", "radarr", 99)
        resolved = reconcile_pending_delete_intents(conn)
        assert resolved >= 1
        row = conn.execute(
            "SELECT completed_at FROM delete_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        assert row["completed_at"] is not None

    def test_reconcile_removes_orphaned_media_row(self, conn):
        """An unresolved intent for an existing media row is cleaned up."""
        _insert_movie(conn, "m2", radarr_id=None)
        _record_delete_intent(conn, "m2", "radarr", "none")

        resolved = reconcile_pending_delete_intents(conn)
        assert resolved >= 1

        # Media row must be gone.
        row = conn.execute("SELECT id FROM media_items WHERE id = 'm2'").fetchone()
        assert row is None

    def test_reconcile_is_idempotent(self, conn):
        """Running reconcile twice is safe; second run resolves nothing new."""
        _record_delete_intent(conn, "gone-m3", "radarr", 42)

        first = reconcile_pending_delete_intents(conn)
        second = reconcile_pending_delete_intents(conn)

        # Second run should find nothing to do.
        assert second == 0
        assert first >= 1

    def test_reconcile_retries_an_intent_whose_cleanup_failed(self, conn):
        """A crashed intent whose startup cleanup errors stays pending for the next start."""
        _insert_movie(conn, "m4", radarr_id=None)
        _record_delete_intent(conn, "m4", "radarr", "none")

        with patch("mediaman.core.audit.log_audit", side_effect=sqlite3.OperationalError("busy")):
            assert reconcile_pending_delete_intents(conn) == 0

        assert reconcile_pending_delete_intents(conn) == 1
        assert conn.execute("SELECT id FROM media_items WHERE id = 'm4'").fetchone() is None


def _audit_actions(conn, media_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT action FROM audit_log WHERE media_item_id = ?", (media_id,)
    ).fetchall()
    return [r["action"] for r in rows]


def _assert_kept_after_restart(conn, media_id: str) -> None:
    reconcile_pending_delete_intents(conn)
    assert conn.execute("SELECT id FROM media_items WHERE id = ?", (media_id,)).fetchone()
    assert conn.execute(
        "SELECT id FROM scheduled_actions WHERE media_item_id = ?", (media_id,)
    ).fetchone()
    actions = _audit_actions(conn, media_id)
    assert "deleted" not in actions
    assert "delete_failed" in actions


class TestFailedArrDeleteSurvivesRestart:
    """A delete the Arr refused is never booked as deleted by the startup reconcile."""

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        _DELETE_LIMITER.reset()

    def test_failed_radarr_delete_keeps_row_and_its_scheduled_action_after_restart(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn)
        insert_scheduled_action(conn, media_item_id="m1", action=ACTION_SCHEDULED_DELETION)
        client = authed_client(_app(app_factory, conn), conn)

        mock_radarr = MagicMock()
        mock_radarr.delete_movie.side_effect = SafeHTTPError(
            status_code=500, body_snippet="boom", url="http://radarr/"
        )
        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            assert client.post("/api/media/m1/delete").status_code == 502

        _assert_kept_after_restart(conn, "m1")

    def test_sonarr_failing_after_removing_season_files_keeps_row_after_restart(
        self, app_factory, authed_client, conn
    ):
        """Files gone but Sonarr errored before confirming: the row stays for a retry."""
        _insert_tv_season(conn)
        insert_scheduled_action(conn, media_item_id="s1", action=ACTION_SCHEDULED_DELETION)
        client = authed_client(_app(app_factory, conn), conn)

        mock_sonarr = MagicMock()
        mock_sonarr.unmonitor_season.side_effect = requests.ConnectionError("refused")
        with patch(
            "mediaman.web.routes.library_api.build_sonarr_from_db", return_value=mock_sonarr
        ):
            assert client.post("/api/media/s1/delete").status_code == 502

        mock_sonarr.delete_episode_files.assert_called_once()
        _assert_kept_after_restart(conn, "s1")
        detail = conn.execute(
            "SELECT detail FROM audit_log WHERE media_item_id = 's1' AND action = 'delete_failed'"
        ).fetchone()["detail"]
        assert "files" in detail

    def test_unexpected_arr_error_keeps_row_after_restart(self, app_factory, authed_client, conn):
        _insert_movie(conn)
        insert_scheduled_action(conn, media_item_id="m1", action=ACTION_SCHEDULED_DELETION)
        client = authed_client(_app(app_factory, conn), conn)

        mock_radarr = MagicMock()
        mock_radarr.delete_movie.side_effect = KeyError("id")
        with (
            patch("mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr),
            pytest.raises(KeyError),
        ):
            client.post("/api/media/m1/delete")

        _assert_kept_after_restart(conn, "m1")

    def test_intent_left_pending_with_an_error_by_older_versions_is_not_booked_deleted(self, conn):
        _insert_movie(conn)
        insert_scheduled_action(conn, media_item_id="m1", action=ACTION_PROTECTED_FOREVER)
        intent_id = _record_delete_intent(conn, "m1", "radarr", 101)
        conn.execute("UPDATE delete_intents SET last_error = 'HTTP 500' WHERE id = ?", (intent_id,))
        conn.commit()

        _assert_kept_after_restart(conn, "m1")
        assert reconcile_pending_delete_intents(conn) == 0
