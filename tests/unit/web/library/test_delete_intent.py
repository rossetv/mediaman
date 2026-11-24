from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.core.scheduled_action_kinds import ACTION_PROTECTED_FOREVER, ACTION_SCHEDULED_DELETION
from mediaman.core.time import now_utc
from mediaman.scanner.phases.delete import remove_orphans
from mediaman.services.infra import SafeHTTPError
from mediaman.web.repository.delete_intents import build_verify_gone
from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.library_api import (
    _DELETE_LIMITER,
    _complete_delete_intent,
    _record_delete_intent,
    reconcile_pending_delete_intents,
)
from mediaman.web.routes.library_api import delete as delete_route
from mediaman.web.routes.library_api import router as library_api_router
from tests.helpers.factories import insert_scheduled_action
from tests.helpers.library_index import library_movies
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

    def test_a_deleted_title_leaves_the_held_library_index_at_once(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn)
        assert library_movies(conn) == {550: "movie-m1"}
        client = authed_client(_app(app_factory, conn), conn)

        with patch(RADARR, return_value=_radarr()):
            assert client.post("/api/media/m1/delete").status_code == 200

        assert library_movies(conn) == {}

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

    def test_a_title_verified_gone_leaves_the_held_library_index_at_once(self, conn):
        _insert_movie(conn, "m2", radarr_id=None)
        _record_delete_intent(conn, "m2", "radarr", "none")
        assert library_movies(conn) == {550: "movie-m2"}

        reconcile_pending_delete_intents(conn, verify_gone=lambda _i: True)

        assert library_movies(conn) == {}

    def test_verified_gone_is_not_booked_while_a_tracked_file_is_still_on_disk(
        self, conn, tmp_path
    ):
        kept = tmp_path / "Test Movie (2020).mkv"
        kept.write_bytes(b"x")
        _insert_movie(conn, "m2", tracked_files=[{"path": str(kept), "size": 1}])
        intent_id = _record_delete_intent(conn, "m2", "radarr", "101")

        assert reconcile_pending_delete_intents(conn, verify_gone=lambda _i: True) == 1

        assert conn.execute("SELECT id FROM media_items WHERE id = 'm2'").fetchone()
        intent = conn.execute(
            "SELECT completed_at, last_error FROM delete_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        assert intent["completed_at"] is not None
        assert "Radarr no longer has it" in intent["last_error"]
        assert "press Delete again" not in intent["last_error"]
        assert _audit_actions(conn, "m2") == ["delete_failed"]

    def test_verified_gone_is_not_booked_when_the_media_folder_looks_unmounted(
        self, conn, tmp_path
    ):
        root = tmp_path / "movies"
        root.mkdir()
        gone = [{"path": str(root / "Test Movie" / "movie.mkv"), "size": 1}]
        _insert_movie(conn, "m2", tracked_files=gone)
        intent_id = _record_delete_intent(conn, "m2", "radarr", "101")

        reconcile_pending_delete_intents(
            conn, verify_gone=lambda _i: True, delete_roots=[str(root)]
        )

        assert conn.execute("SELECT id FROM media_items WHERE id = 'm2'").fetchone()
        intent = conn.execute(
            "SELECT last_error FROM delete_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        assert "may not be mounted" in intent["last_error"]
        assert _audit_actions(conn, "m2") == ["delete_failed"]

    def test_verified_gone_is_booked_when_the_media_folder_has_content(self, conn, tmp_path):
        root = tmp_path / "movies"
        root.mkdir()
        (root / "other.mkv").write_bytes(b"x")
        gone = [{"path": str(root / "Test Movie" / "movie.mkv"), "size": 1}]
        _insert_movie(conn, "m2", tracked_files=gone)
        _record_delete_intent(conn, "m2", "radarr", "101")

        reconcile_pending_delete_intents(
            conn, verify_gone=lambda _i: True, delete_roots=[str(root)]
        )

        assert conn.execute("SELECT id FROM media_items WHERE id = 'm2'").fetchone() is None

    def test_verified_gone_season_is_not_booked_while_an_episode_is_still_on_disk(
        self, conn, tmp_path
    ):
        left = tmp_path / "S01E02.mkv"
        left.write_bytes(b"x")
        files = [
            {"path": str(tmp_path / "S01E01.mkv"), "size": 1},
            {"path": str(left), "size": 1},
        ]
        _insert_tv_season(conn, "s2", tracked_files=files)
        _record_delete_intent(conn, "s2", "sonarr", "202")

        assert reconcile_pending_delete_intents(conn, verify_gone=lambda _i: True) == 1

        assert conn.execute("SELECT id FROM media_items WHERE id = 's2'").fetchone()
        assert "deleted" not in _audit_actions(conn, "s2")

    def test_verified_gone_is_booked_when_the_tracked_files_are_gone_locally(self, conn, tmp_path):
        gone = [{"path": str(tmp_path / "gone.mkv"), "size": 1}]
        _insert_movie(conn, "m2", tracked_files=gone)
        _record_delete_intent(conn, "m2", "radarr", "101")

        assert reconcile_pending_delete_intents(conn, verify_gone=lambda _i: True) == 1

        assert conn.execute("SELECT id FROM media_items WHERE id = 'm2'").fetchone() is None

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

    def test_a_delete_whose_cleanup_failed_is_booked_though_plex_drops_the_item_first(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)
        with (
            patch(RADARR, return_value=_radarr()),
            patch(
                "mediaman.web.routes.library_api.delete.finalise_delete_in_tx",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            pytest.raises(sqlite3.OperationalError),
        ):
            client.post("/api/media/m1/delete")
        library = conn.execute("SELECT plex_library_id FROM media_items").fetchone()[0]
        listed = {f"other-{i}" for i in range(5)}

        remove_orphans(conn, listed, {library})
        reconcile_pending_delete_intents(conn, verify_gone=lambda _i: True)

        assert "deleted" in _audit_actions(conn, "m1")
        assert conn.execute("SELECT title FROM deleted_media").fetchone()
        assert conn.execute("SELECT 1 FROM media_items").fetchone() is None

    def test_a_delete_whose_intent_cannot_be_completed_is_booked_once(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn)
        client = authed_client(_app(app_factory, conn), conn)
        conn.execute(
            "CREATE TRIGGER busy BEFORE UPDATE OF completed_at ON delete_intents "
            "BEGIN SELECT RAISE(ABORT, 'busy'); END"
        )
        with patch(RADARR, return_value=_radarr()), pytest.raises(sqlite3.IntegrityError):
            client.post("/api/media/m1/delete")
        conn.execute("DROP TRIGGER busy")
        # Plex lists the title again before the intent is closed.
        conn.execute("DELETE FROM media_items WHERE id = 'm1'")
        _insert_movie(conn)

        reconcile_pending_delete_intents(conn)

        assert _audit_actions(conn, "m1").count("deleted") == 1

    def test_a_delete_the_reconcile_books_first_is_not_booked_again(
        self, app_factory, authed_client, conn, monkeypatch
    ):
        _insert_movie(conn)
        client = authed_client(_app(app_factory, conn), conn)
        real = delete_route._confirm_delete_intent

        def confirm_then_reconcile(conn, intent_id):
            real(conn, intent_id)
            reconcile_pending_delete_intents(conn)

        monkeypatch.setattr(delete_route, "_confirm_delete_intent", confirm_then_reconcile)
        with patch(RADARR, return_value=_radarr()):
            assert client.post("/api/media/m1/delete").status_code == 200

        assert _audit_actions(conn, "m1").count("deleted") == 1

    def _delete_season_with_failing_cleanup(self, app_factory, authed_client, conn):
        client = authed_client(_app(app_factory, conn), conn)
        with (
            patch(SONARR, return_value=_sonarr()),
            patch(
                "mediaman.web.routes.library_api.delete.finalise_delete_in_tx",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            pytest.raises(sqlite3.OperationalError),
        ):
            client.post("/api/media/s1/delete")

    def test_a_season_whose_cleanup_failed_is_booked_though_sonarr_keeps_the_show(
        self, app_factory, authed_client, conn
    ):
        _insert_tv_season(conn)
        self._delete_season_with_failing_cleanup(app_factory, authed_client, conn)

        for _ in range(2):
            reconcile_pending_delete_intents(conn, verify_gone=lambda _i: False)

        booked = conn.execute(
            "SELECT space_reclaimed_bytes, tvdb_id, season_number FROM audit_log a "
            "JOIN deleted_media d ON d.audit_id = a.id WHERE media_item_id = 's1'"
        ).fetchall()
        assert [tuple(r) for r in booked] == [(2_000_000, 81189, 1)]
        assert conn.execute("SELECT 1 FROM media_items").fetchone() is None

    def test_a_new_copy_since_the_confirmed_delete_stays_with_its_keep_and_no_schedule(
        self, app_factory, authed_client, conn, tmp_path
    ):
        _insert_tv_season(conn)
        self._delete_season_with_failing_cleanup(app_factory, authed_client, conn)
        new = tmp_path / "S01E01.mkv"
        new.write_bytes(b"x")
        conn.execute(
            "UPDATE media_items SET tracked_files = ?",
            (json.dumps([{"path": str(new), "size": 1}]),),
        )
        insert_scheduled_action(conn, media_item_id="s1", action=ACTION_SCHEDULED_DELETION)
        insert_scheduled_action(
            conn, media_item_id="s1", action=ACTION_PROTECTED_FOREVER, token="kept"
        )
        conn.commit()

        for _ in range(2):
            reconcile_pending_delete_intents(conn, verify_gone=lambda _i: False)

        assert _audit_actions(conn, "s1") == ["deleted"]
        assert conn.execute("SELECT 1 FROM media_items").fetchone()
        actions = [r[0] for r in conn.execute("SELECT action FROM scheduled_actions")]
        assert actions == [ACTION_PROTECTED_FOREVER]

    def test_a_confirmation_that_cannot_be_written_does_not_stop_the_delete(
        self, app_factory, authed_client, conn
    ):
        _insert_tv_season(conn)
        conn.execute(
            "CREATE TRIGGER no_confirm BEFORE UPDATE OF confirmed_at ON delete_intents "
            "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
        client = authed_client(_app(app_factory, conn), conn)
        with patch(SONARR, return_value=_sonarr()):
            assert client.post("/api/media/s1/delete").status_code == 200

        assert "deleted" in _audit_actions(conn, "s1")

    def test_an_item_whose_delete_was_closed_goes_when_plex_drops_it(self, conn):
        _insert_movie(conn)
        _complete_delete_intent(conn, _record_delete_intent(conn, "m1", "radarr", "42"))
        library = conn.execute("SELECT plex_library_id FROM media_items").fetchone()[0]

        remove_orphans(conn, {f"other-{i}" for i in range(5)}, {library})

        assert conn.execute("SELECT 1 FROM media_items").fetchone() is None

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
            ({"return_value": {"id": 101, "hasFile": False}}, False),
            ({"return_value": {"id": 101, "hasFile": True}}, False),
            ({"return_value": {"id": 101}}, False),
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
        ("answer", "expected"),
        [
            ({"side_effect": SafeHTTPError(404, "Not Found", "sonarr/")}, True),
            ({"return_value": {"id": 202, "seasons": [{"seasonNumber": 1}]}}, False),
            ({"side_effect": requests.ConnectionError("refused")}, None),
        ],
        ids=["404", "show-listed", "unreachable"],
    )
    def test_season(self, conn, answer, expected):
        _insert_tv_season(conn)
        sonarr = MagicMock()
        sonarr.get_series_by_id.configure_mock(**answer)
        sonarr.get_episode_files.return_value = []

        with patch(VERIFY_SONARR, return_value=sonarr):
            result = build_verify_gone(conn, "key")(_intent(conn, "sonarr", "202", "s1"))

        assert result is expected
        sonarr.get_series_by_id.assert_called_once_with(202)

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

    def test_background_job_passes_the_stored_delete_roots_to_the_reconcile(self, conn):
        from mediaman import app_factory

        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('delete_allowed_roots', ?, 0, '2026-01-01T00:00:00+00:00')",
            (json.dumps(["/media/a", "/media/b"]),),
        )
        conn.commit()

        with (
            patch("mediaman.db.get_db", return_value=conn),
            patch("mediaman.db.close_db"),
            patch(
                "mediaman.web.repository.delete_intents.reconcile_pending_delete_intents",
                return_value=0,
            ) as reconcile,
        ):
            app_factory._reconcile_delete_intents_in_background("key", "2026-01-01T00:00:00+00:00")

        assert reconcile.call_args.kwargs["delete_roots"] == ["/media/a", "/media/b"]

    def test_background_job_still_reconciles_with_no_roots_when_they_cannot_be_read(self, conn):
        from mediaman import app_factory

        with (
            patch("mediaman.db.get_db", return_value=conn),
            patch("mediaman.db.close_db"),
            patch(
                "mediaman.scanner.repository.read_delete_allowed_roots_setting",
                side_effect=RuntimeError("settings unreadable"),
            ),
            patch(
                "mediaman.web.repository.delete_intents.reconcile_pending_delete_intents",
                return_value=0,
            ) as reconcile,
        ):
            app_factory._reconcile_delete_intents_in_background("key", "2026-01-01T00:00:00+00:00")

        reconcile.assert_called_once()
        assert reconcile.call_args.kwargs["delete_roots"] == []

    def test_background_job_closes_its_connection_even_when_reconcile_fails(self, conn):
        from mediaman import app_factory

        with (
            patch("mediaman.db.get_db", return_value=conn),
            patch("mediaman.db.close_db") as close_db,
            patch(
                "mediaman.web.repository.delete_intents.reconcile_pending_delete_intents",
                side_effect=RuntimeError("boom"),
            ),
        ):
            app_factory._reconcile_delete_intents_in_background("key", "2026-01-01T00:00:00+00:00")

        close_db.assert_called_once()

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


def _add_progress(conn, rating_key: str, media_item_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO admin_users (username, password_hash, created_at, role)"
        " VALUES ('aoife', 'x', '2020', 'viewer')"
    )
    conn.execute(
        "INSERT INTO watch_progress (username, rating_key, media_item_id, updated_at)"
        " VALUES ('aoife', ?, ?, '2020-01-01T00:00:00+00:00')",
        (rating_key, media_item_id),
    )
    conn.commit()


def _progress_keys(conn) -> list[str]:
    return [r["rating_key"] for r in conn.execute("SELECT rating_key FROM watch_progress")]


def test_reconciling_a_verified_gone_season_drops_its_episodes_progress(conn):
    _insert_tv_season(conn, "s1", sonarr_id=None)
    _insert_tv_season(conn, "s2", sonarr_id=None, season=2)
    _add_progress(conn, "ep1", "s1")
    _add_progress(conn, "ep2", "s1")
    _add_progress(conn, "other-ep", "s2")
    _record_delete_intent(conn, "s1", "sonarr", "none")

    assert reconcile_pending_delete_intents(conn, verify_gone=lambda _i: True) == 1

    assert _progress_keys(conn) == ["other-ep"]


def test_finishing_a_season_delete_drops_its_episodes_progress(conn):
    from mediaman.web.repository.library_api import MediaDeleteSnapshot, finalise_delete_in_tx

    _insert_tv_season(conn, "s1")
    _insert_tv_season(conn, "s2", season=2)
    _add_progress(conn, "ep1", "s1")
    _add_progress(conn, "ep2", "s1")
    _add_progress(conn, "other-ep", "s2")
    snapshot = MediaDeleteSnapshot(
        title="The Show",
        media_type="tv_season",
        file_path=None,
        file_size_bytes=None,
        radarr_id=None,
        sonarr_id=202,
        season_number=1,
        plex_rating_key="rk-s1",
        show_rating_key="show-rk",
        tmdb_id=None,
        tvdb_id=81189,
        tracked_files=None,
    )

    finalise_delete_in_tx(
        conn,
        snapshot,
        media_id="s1",
        intent_id=_record_delete_intent(conn, "s1", "sonarr", "202"),
        audit_detail="Deleted: The Show",
        actor="admin",
    )

    assert _progress_keys(conn) == ["other-ep"]
