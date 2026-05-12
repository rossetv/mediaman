"""Tests for the execute_deletions method of the scan engine."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.db import init_db
from mediaman.scanner.engine import ScanEngine
from tests.helpers.factories import insert_media_item, insert_scheduled_action


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


@pytest.fixture
def mock_plex():
    client = MagicMock()
    client.get_movie_items.return_value = []
    client.get_show_seasons.return_value = []
    client.get_watch_history.return_value = []
    client.get_season_watch_history.return_value = []
    return client


class TestExecuteDeletions:
    """Tests for the execute_deletions method."""

    def _insert_item(self, conn, item_id, title, file_path="/tmp/fake", file_size=1_000_000):
        insert_media_item(
            conn,
            id=item_id,
            title=title,
            plex_rating_key=item_id,
            file_path=file_path,
            file_size_bytes=file_size,
        )

    def _insert_scheduled_deletion(self, conn, item_id, execute_at):
        insert_scheduled_action(
            conn,
            media_item_id=item_id,
            action="scheduled_deletion",
            token=f"tok-{item_id}",
            execute_at=execute_at,
            token_used=False,
        )

    def test_dry_run_does_not_delete_files(self, conn, mock_plex, freezer):
        """dry_run=True logs dry_run_skip but does not delete or remove the action row."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        self._insert_item(conn, "700", "Dry Run Movie")
        self._insert_scheduled_deletion(conn, "700", past)

        with patch("mediaman.services.infra.storage.delete_path") as mock_delete:
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="test-key",
                dry_run=True,
            )
            result = engine.execute_deletions()

        mock_delete.assert_not_called()
        assert result["deleted"] == 0

        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='700'").fetchone()
        assert row is not None

        log = conn.execute("SELECT * FROM audit_log WHERE media_item_id='700'").fetchone()
        assert log is not None
        assert log["action"] == "dry_run_skip"

    def test_execute_deletes_past_due_items(self, conn, mock_plex, monkeypatch, freezer):
        """Items whose execute_at has passed are deleted and the action row removed."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        self._insert_item(conn, "800", "Deletable Movie", file_size=2_000_000)
        self._insert_scheduled_deletion(conn, "800", past)

        with patch("mediaman.scanner.deletions.delete_path") as mock_delete:
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="test-key",
            )
            result = engine.execute_deletions()

        mock_delete.assert_called_once_with("/tmp/fake", allowed_roots=["/tmp"])
        assert result["deleted"] == 1
        assert result["reclaimed_bytes"] == 2_000_000

        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='800'").fetchone()
        assert row is None

        log = conn.execute("SELECT * FROM audit_log WHERE media_item_id='800'").fetchone()
        assert log is not None
        assert log["action"] == "deleted"
        assert log["space_reclaimed_bytes"] == 2_000_000

    def test_future_deletions_not_executed(self, conn, mock_plex, freezer):
        """Items whose execute_at is in the future are not touched."""
        now = datetime.now(UTC)
        future = (now + timedelta(days=7)).isoformat()

        self._insert_item(conn, "900", "Future Movie")
        self._insert_scheduled_deletion(conn, "900", future)

        with patch("mediaman.scanner.deletions.delete_path") as mock_delete:
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="test-key",
            )
            result = engine.execute_deletions()

        mock_delete.assert_not_called()
        assert result["deleted"] == 0

    def test_radarr_unmonitor_called(self, conn, mock_plex, monkeypatch, freezer):
        """execute_deletions calls radarr.unmonitor_movie when radarr_id is set."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        insert_media_item(
            conn,
            id="910",
            title="Radarr Movie",
            plex_rating_key="910",
            file_path="/tmp/fake",
            file_size_bytes=500_000,
            radarr_id=42,
        )
        self._insert_scheduled_deletion(conn, "910", past)

        mock_radarr = MagicMock()

        with patch("mediaman.scanner.deletions.delete_path"):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="test-key",
                radarr_client=mock_radarr,
            )
            engine.execute_deletions()

        mock_radarr.unmonitor_movie.assert_called_once_with(42)

    def test_sonarr_unmonitor_called(self, conn, mock_plex, monkeypatch, freezer):
        """execute_deletions calls sonarr.unmonitor_season when sonarr_id + season_number set."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        insert_media_item(
            conn,
            id="920",
            title="Sonarr Show S1",
            media_type="season",
            plex_rating_key="920",
            file_path="/tmp/fake",
            file_size_bytes=800_000,
            sonarr_id=99,
            season_number=1,
        )
        self._insert_scheduled_deletion(conn, "920", past)

        mock_sonarr = MagicMock()

        with patch("mediaman.scanner.deletions.delete_path"):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="test-key",
                sonarr_client=mock_sonarr,
            )
            engine.execute_deletions()

        mock_sonarr.unmonitor_season.assert_called_once_with(99, 1)

    def test_arr_failure_does_not_abort_deletion(self, conn, mock_plex, monkeypatch, freezer):
        """A crash in radarr.unmonitor_movie does not prevent the item being deleted."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        insert_media_item(
            conn,
            id="930",
            title="Exploding Movie",
            plex_rating_key="930",
            file_path="/tmp/fake",
            file_size_bytes=100_000,
            radarr_id=7,
        )
        self._insert_scheduled_deletion(conn, "930", past)

        mock_radarr = MagicMock()
        mock_radarr.unmonitor_movie.side_effect = requests.ConnectionError("radarr down")

        with patch("mediaman.scanner.deletions.delete_path"):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="test-key",
                radarr_client=mock_radarr,
            )
            result = engine.execute_deletions()

        assert result["deleted"] == 1

    def test_scanner_skips_deletion_when_no_allowed_roots_configured(
        self,
        conn,
        mock_plex,
        monkeypatch,
        caplog,
        freezer,
    ):
        """With no roots configured, the scheduled deletion is skipped and left
        intact so a later run (once the admin sets a root) can execute it."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        self._insert_item(conn, "880", "No Roots Movie")
        self._insert_scheduled_deletion(conn, "880", past)

        with patch("mediaman.scanner.deletions.delete_path") as mock_delete:
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="test-key",
            )
            with caplog.at_level("ERROR", logger="mediaman"):
                result = engine.execute_deletions()

        mock_delete.assert_not_called()
        assert result["deleted"] == 0

        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='880'").fetchone()
        assert row is not None

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "delete_allowed_roots" in messages
        assert "MEDIAMAN_DELETE_ROOTS" in messages

    def test_scanner_continues_after_skip_when_one_item_has_bad_root(
        self,
        conn,
        mock_plex,
        monkeypatch,
        freezer,
    ):
        """If the allowlist is set but a single item's path is outside it,
        only that item is skipped — other deletions still proceed."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        self._insert_item(conn, "881", "Good Root", file_path="/tmp/fake")
        self._insert_scheduled_deletion(conn, "881", past)
        self._insert_item(conn, "882", "Bad Root", file_path="/etc/passwd")
        self._insert_scheduled_deletion(conn, "882", past)

        def fake_delete(path, *, allowed_roots):
            if path == "/etc/passwd":
                raise ValueError("outside allowed roots")

        with patch("mediaman.scanner.deletions.delete_path", side_effect=fake_delete):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="test-key",
            )
            result = engine.execute_deletions()

        assert result["deleted"] == 1

    def test_expired_snoozes_are_cleaned_up(self, conn, mock_plex, freezer):
        """Snoozed rows with a past execute_at are deleted so items re-enter the pipeline."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        self._insert_item(conn, "940", "Snoozed Item")
        insert_scheduled_action(
            conn, media_item_id="940", action="snoozed", execute_at=past, token="tok-snooze-940"
        )

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[],
            library_types={},
            secret_key="test-key",
        )
        engine.execute_deletions()

        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='940'").fetchone()
        assert row is None

    def test_future_snoozes_are_preserved(self, conn, mock_plex, freezer):
        """Active snoozes (execute_at in the future) are not cleaned up."""
        now = datetime.now(UTC)
        future = (now + timedelta(days=7)).isoformat()

        self._insert_item(conn, "950", "Active Snooze Item")
        insert_scheduled_action(
            conn, media_item_id="950", action="snoozed", execute_at=future, token="tok-snooze-950"
        )

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[],
            library_types={},
            secret_key="test-key",
        )
        engine.execute_deletions()

        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='950'").fetchone()
        assert row is not None
