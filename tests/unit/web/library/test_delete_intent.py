from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.core.scheduled_action_kinds import ACTION_PROTECTED_FOREVER, ACTION_SCHEDULED_DELETION
from mediaman.core.time import now_utc
from mediaman.services.infra import SafeHTTPError
from mediaman.web.repository.delete_intents import build_verify_gone
from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.library_api import (
    _DELETE_LIMITER,
    _complete_delete_intent,
    _record_delete_intent,
    reconcile_pending_delete_intents,
)
from mediaman.web.routes.library_api import router as library_api_router
from tests.helpers.factories import insert_scheduled_action
from tests.unit.web.test_library_mutations import (
    RADARR,
    SONARR,
    _insert_movie,
    _insert_tv_season,
    _radarr,
    _sonarr,
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

        mock_radarr = _radarr()

        with patch(RADARR, return_value=mock_radarr):
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

        mock_radarr = _radarr()
        with patch(RADARR, return_value=mock_radarr):
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

        mock_radarr = _radarr()
        mock_radarr.delete_movie.side_effect = SafeHTTPError(
            status_code=0, body_snippet="transport error: ConnectionError", url="http://radarr/"
        )

        with patch(RADARR, return_value=mock_radarr):
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

    def test_interrupted_intent_is_failed_and_keeps_the_media_row(self, conn):
        """A delete cut off before the Arr confirmed it is not booked as deleted."""
        _insert_movie(conn, "m2", radarr_id=None)
        intent_id = _record_delete_intent(conn, "m2", "radarr", "none")

        assert reconcile_pending_delete_intents(conn) == 1

        assert conn.execute("SELECT id FROM media_items WHERE id = 'm2'").fetchone()
        intent = conn.execute(
            "SELECT completed_at, last_error FROM delete_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        assert intent["completed_at"] is not None
        assert intent["last_error"] == (
            "Delete interrupted; not confirmed by Radarr/Sonarr \u2014 press Delete again"
        )
        actions = _audit_actions(conn, "m2")
        assert "deleted" not in actions
        assert actions == ["delete_failed"]

    def test_interrupted_intent_is_failed_when_verification_cannot_tell(self, conn):
        _insert_movie(conn, "m2", radarr_id=None)
        _record_delete_intent(conn, "m2", "radarr", "none")

        assert reconcile_pending_delete_intents(conn, verify_gone=lambda _i: None) == 1

        assert conn.execute("SELECT id FROM media_items WHERE id = 'm2'").fetchone()
        assert "deleted" not in _audit_actions(conn, "m2")

    def test_interrupted_intent_is_booked_deleted_when_verified_gone(self, conn):
        _insert_movie(conn, "m2", radarr_id=None)
        _record_delete_intent(conn, "m2", "radarr", "none")

        assert reconcile_pending_delete_intents(conn, verify_gone=lambda _i: True) == 1

        assert conn.execute("SELECT id FROM media_items WHERE id = 'm2'").fetchone() is None
        audit = conn.execute(
            "SELECT id FROM audit_log WHERE media_item_id = 'm2' AND action = 'deleted'"
        ).fetchone()
        deleted = conn.execute(
            "SELECT media_type, tmdb_id, title FROM deleted_media WHERE audit_id = ?",
            (audit["id"],),
        ).fetchone()
        assert tuple(deleted) == ("movie", 550, "Test Movie")

    def test_intent_started_after_the_cutoff_is_left_for_its_request(self, conn):
        """A run overlapping live requests must not fail a delete still in flight."""
        _insert_movie(conn, "m2", radarr_id=None)
        intent_id = _record_delete_intent(conn, "m2", "radarr", "none")

        assert (
            reconcile_pending_delete_intents(conn, started_before="2000-01-01T00:00:00+00:00") == 0
        )

        intent = conn.execute(
            "SELECT completed_at FROM delete_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        assert intent["completed_at"] is None
        assert _audit_actions(conn, "m2") == []
        assert (
            reconcile_pending_delete_intents(conn, started_before="9999-01-01T00:00:00+00:00") == 1
        )

    def test_reconcile_is_idempotent(self, conn):
        """Running reconcile twice is safe; second run resolves nothing new."""
        _record_delete_intent(conn, "gone-m3", "radarr", 42)

        first = reconcile_pending_delete_intents(conn)
        second = reconcile_pending_delete_intents(conn)

        # Second run should find nothing to do.
        assert second == 0
        assert first >= 1

    def test_reconcile_retries_an_intent_whose_cleanup_failed(self, conn):
        """A crashed intent whose reconcile errors stays pending for the next run."""
        _insert_movie(conn, "m4", radarr_id=None)
        _record_delete_intent(conn, "m4", "radarr", "none")

        with patch("mediaman.core.audit.log_audit", side_effect=sqlite3.OperationalError("busy")):
            assert reconcile_pending_delete_intents(conn) == 0

        assert reconcile_pending_delete_intents(conn) == 1
        assert conn.execute("SELECT id FROM media_items WHERE id = 'm4'").fetchone()


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

        mock_radarr = _radarr()
        mock_radarr.delete_movie.side_effect = SafeHTTPError(
            status_code=500, body_snippet="boom", url="http://radarr/"
        )
        with patch(RADARR, return_value=mock_radarr):
            assert client.post("/api/media/m1/delete").status_code == 502

        _assert_kept_after_restart(conn, "m1")

    def test_sonarr_failing_after_removing_season_files_keeps_row_after_restart(
        self, app_factory, authed_client, conn
    ):
        """Files gone but Sonarr errored before confirming: the row stays for a retry."""
        _insert_tv_season(conn)
        insert_scheduled_action(conn, media_item_id="s1", action=ACTION_SCHEDULED_DELETION)
        client = authed_client(_app(app_factory, conn), conn)

        mock_sonarr = _sonarr()
        mock_sonarr.unmonitor_season.side_effect = requests.ConnectionError("refused")
        with patch(SONARR, return_value=mock_sonarr):
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

        mock_radarr = _radarr()
        mock_radarr.delete_movie.side_effect = KeyError("id")
        with (
            patch(RADARR, return_value=mock_radarr),
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


def _intent(conn, kind: str, target_id: str, media_id: str):
    intent_id = _record_delete_intent(conn, media_id, kind, target_id)
    return conn.execute(
        "SELECT id, media_item_id, target_kind, target_id, last_error "
        "FROM delete_intents WHERE id = ?",
        (intent_id,),
    ).fetchone()


VERIFY_RADARR = "mediaman.web.repository.delete_intents.build_radarr_from_db"
VERIFY_SONARR = "mediaman.web.repository.delete_intents.build_sonarr_from_db"


class TestBuildVerifyGone:
    """Only a clear answer from Radarr/Sonarr books an interrupted delete as done."""

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            ({"side_effect": SafeHTTPError(404, "Not Found", "radarr/")}, True),
            ({"return_value": {"id": 101, "hasFile": False}}, True),
            ({"return_value": {"id": 101, "hasFile": True}}, False),
            ({"return_value": {"id": 101}}, None),
            ({"side_effect": requests.ConnectionError("refused")}, None),
            ({"side_effect": SafeHTTPError(500, "boom", "radarr/")}, None),
        ],
        ids=["404", "no-file", "has-file", "unclear", "unreachable", "server-error"],
    )
    def test_movie(self, conn, answer, expected):
        _insert_movie(conn)
        radarr = MagicMock()
        radarr.get_movie_by_id.configure_mock(**answer)

        with patch(VERIFY_RADARR, return_value=radarr):
            result = build_verify_gone(conn, "key")(_intent(conn, "radarr", "101", "m1"))

        assert result is expected
        radarr.get_movie_by_id.assert_called_once_with(101)

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            ([{"id": 3, "seasonNumber": 2}], True),
            ([{"id": 1, "seasonNumber": 1}, {"id": 3, "seasonNumber": 2}], False),
            (requests.ConnectionError("refused"), None),
        ],
        ids=["season-empty", "season-has-files", "unreachable"],
    )
    def test_season(self, conn, files, expected):
        _insert_tv_season(conn)
        sonarr = MagicMock()
        if isinstance(files, Exception):
            sonarr.get_episode_files.side_effect = files
        else:
            sonarr.get_episode_files.return_value = files

        with patch(VERIFY_SONARR, return_value=sonarr):
            result = build_verify_gone(conn, "key")(_intent(conn, "sonarr", "202", "s1"))

        assert result is expected

    def test_arr_not_configured_cannot_tell(self, conn):
        _insert_movie(conn)
        with patch(VERIFY_RADARR, return_value=None):
            assert build_verify_gone(conn, "key")(_intent(conn, "radarr", "101", "m1")) is None

    def test_intent_without_a_usable_target_id_cannot_tell(self, conn):
        _insert_movie(conn)
        radarr = MagicMock()
        with patch(VERIFY_RADARR, return_value=radarr):
            assert build_verify_gone(conn, "key")(_intent(conn, "radarr", "none", "m1")) is None
        radarr.get_movie_by_id.assert_not_called()


class TestStartupReconciliationIsOffTheStartupPath:
    def test_startup_returns_without_reconciling_inline(self):
        from mediaman import app_factory

        app = MagicMock()
        with (
            patch.object(app_factory.threading, "Thread") as thread_cls,
            patch(
                "mediaman.web.repository.delete_intents.reconcile_pending_delete_intents"
            ) as reconcile,
            patch(
                "mediaman.services.downloads.notifications.reconcile_stranded_notifications",
                return_value=0,
            ),
        ):
            app_factory._run_startup_reconciliation(app)

        reconcile.assert_not_called()
        assert thread_cls.call_args.kwargs["daemon"] is True
        secret_key, started_before = thread_cls.call_args.kwargs["args"]
        assert secret_key is app.state.config.secret_key
        assert started_before <= now_utc().isoformat()
        thread_cls.return_value.start.assert_called_once()

    def test_background_job_uses_its_own_connection_and_swallows_errors(self, conn):
        from mediaman import app_factory

        with (
            patch("mediaman.db.get_db", return_value=conn) as get_db,
            patch(
                "mediaman.web.repository.delete_intents.reconcile_pending_delete_intents",
                side_effect=RuntimeError("boom"),
            ) as reconcile,
        ):
            app_factory._reconcile_delete_intents_in_background("key", "2026-01-01T00:00:00+00:00")

        get_db.assert_called_once()
        reconcile.assert_called_once()
        assert reconcile.call_args.args == (conn,)
        assert reconcile.call_args.kwargs["started_before"] == "2026-01-01T00:00:00+00:00"

    def test_background_job_books_an_intent_deleted_when_radarr_confirms_it_gone(self, conn):
        from mediaman import app_factory

        _insert_movie(conn)
        _record_delete_intent(conn, "m1", "radarr", "101")
        radarr = MagicMock()
        radarr.get_movie_by_id.side_effect = SafeHTTPError(404, "Not Found", "radarr/")

        with (
            patch("mediaman.db.get_db", return_value=conn),
            patch(VERIFY_RADARR, return_value=radarr),
        ):
            app_factory._reconcile_delete_intents_in_background("key", "9999-01-01T00:00:00+00:00")

        assert conn.execute("SELECT id FROM media_items WHERE id = 'm1'").fetchone() is None
        assert "deleted" in _audit_actions(conn, "m1")
