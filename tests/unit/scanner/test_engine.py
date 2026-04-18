"""Tests for scan engine orchestration."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.scanner.engine import ScanEngine


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


class TestScanEngine:
    def test_scan_schedules_stale_movie(self, conn, mock_plex):
        now = datetime.now(timezone.utc)
        mock_plex.get_movie_items.return_value = [{
            "plex_rating_key": "100",
            "title": "Old Movie",
            "added_at": now - timedelta(days=60),
            "file_path": "/media/movies/Old Movie (2020)",
            "file_size_bytes": 5_000_000_000,
            "poster_path": "/library/metadata/100/thumb/1",
        }]
        mock_plex.get_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
            min_age_days=30,
            inactivity_days=30,
            grace_days=14,
        )
        result = engine.run_scan()

        assert result["scheduled"] == 1
        row = conn.execute("SELECT * FROM scheduled_actions").fetchone()
        assert row is not None
        assert row["action"] == "scheduled_deletion"

    def test_scan_skips_protected_items(self, conn, mock_plex):
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("100", "Protected Movie", "movie", 1, "100",
             (now - timedelta(days=60)).isoformat(),
             "/media/movies/Protected", 5_000_000_000),
        )
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "token, token_used) VALUES (?, ?, ?, ?, ?)",
            ("100", "protected_forever", now.isoformat(), "tok-123", 0),
        )
        conn.commit()

        mock_plex.get_movie_items.return_value = [{
            "plex_rating_key": "100",
            "title": "Protected Movie",
            "added_at": now - timedelta(days=60),
            "file_path": "/media/movies/Protected",
            "file_size_bytes": 5_000_000_000,
            "poster_path": None,
        }]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 0

    def test_scan_creates_media_item_record(self, conn, mock_plex):
        now = datetime.now(timezone.utc)
        mock_plex.get_movie_items.return_value = [{
            "plex_rating_key": "200",
            "title": "New Movie",
            "added_at": now - timedelta(days=5),
            "file_path": "/media/movies/New Movie",
            "file_size_bytes": 3_000_000_000,
            "poster_path": "/library/metadata/200/thumb/1",
        }]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        engine.run_scan()

        row = conn.execute("SELECT * FROM media_items WHERE id='200'").fetchone()
        assert row is not None
        assert row["title"] == "New Movie"

    def test_scan_skips_already_scheduled(self, conn, mock_plex):
        """Items with an existing scheduled_deletion action are not re-scheduled."""
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("300", "Already Scheduled", "movie", 1, "300",
             (now - timedelta(days=90)).isoformat(),
             "/media/movies/Scheduled", 2_000_000_000),
        )
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "token, token_used) VALUES (?, ?, ?, ?, ?)",
            ("300", "scheduled_deletion", now.isoformat(), "tok-already", 0),
        )
        conn.commit()

        mock_plex.get_movie_items.return_value = [{
            "plex_rating_key": "300",
            "title": "Already Scheduled",
            "added_at": now - timedelta(days=90),
            "file_path": "/media/movies/Scheduled",
            "file_size_bytes": 2_000_000_000,
            "poster_path": None,
        }]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 0
        count = conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE media_item_id='300'"
        ).fetchone()[0]
        assert count == 1  # no duplicate inserted

    def test_scan_flags_reentry(self, conn, mock_plex):
        """An item whose snooze has expired is scheduled and marked as re-entry."""
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("400", "Snoozed Movie", "movie", 1, "400",
             (now - timedelta(days=120)).isoformat(),
             "/media/movies/Snoozed", 4_000_000_000),
        )
        # A snooze that has already expired (token_used=1 means acted upon)
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "token, token_used) VALUES (?, ?, ?, ?, ?)",
            ("400", "snoozed", (now - timedelta(days=60)).isoformat(), "tok-old", 1),
        )
        conn.commit()

        mock_plex.get_movie_items.return_value = [{
            "plex_rating_key": "400",
            "title": "Snoozed Movie",
            "added_at": now - timedelta(days=120),
            "file_path": "/media/movies/Snoozed",
            "file_size_bytes": 4_000_000_000,
            "poster_path": None,
        }]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 1
        row = conn.execute(
            "SELECT * FROM scheduled_actions WHERE action='scheduled_deletion'"
        ).fetchone()
        assert row is not None
        assert row["is_reentry"] == 1

    def test_scan_returns_summary_counts(self, conn, mock_plex):
        """run_scan returns a dict with scanned, scheduled, skipped, errors keys."""
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert "scanned" in result
        assert "scheduled" in result
        assert "skipped" in result
        assert result["scanned"] == 0
        assert result["scheduled"] == 0

    def test_scan_tv_season(self, conn, mock_plex):
        """A stale TV season is scheduled for deletion."""
        now = datetime.now(timezone.utc)
        mock_plex.get_show_seasons.return_value = [{
            "plex_rating_key": "500",
            "title": "Breaking Bad",
            "show_title": "Breaking Bad",
            "season_number": 1,
            "added_at": now - timedelta(days=90),
            "file_path": "/media/tv/Breaking Bad/Season 01",
            "file_size_bytes": 10_000_000_000,
            "poster_path": "/library/metadata/500/thumb/1",
            "episode_count": 7,
            "show_rating_key": "501",
        }]
        mock_plex.get_season_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
            min_age_days=30,
            inactivity_days=30,
        )
        result = engine.run_scan()
        assert result["scheduled"] == 1
        row = conn.execute("SELECT * FROM scheduled_actions").fetchone()
        assert row is not None
        assert row["action"] == "scheduled_deletion"

    def test_scan_audit_log_entry(self, conn, mock_plex):
        """Each scheduled item produces an audit_log entry."""
        now = datetime.now(timezone.utc)
        mock_plex.get_movie_items.return_value = [{
            "plex_rating_key": "600",
            "title": "Audit Movie",
            "added_at": now - timedelta(days=60),
            "file_path": "/media/movies/Audit",
            "file_size_bytes": 1_000_000_000,
            "poster_path": None,
        }]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        engine.run_scan()

        row = conn.execute(
            "SELECT * FROM audit_log WHERE media_item_id='600'"
        ).fetchone()
        assert row is not None
        assert row["action"] == "scheduled_deletion"

    def test_scan_returns_deleted_and_reclaimed(self, conn, mock_plex):
        """run_scan summary includes deleted and reclaimed_bytes keys."""
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert "deleted" in result
        assert "reclaimed_bytes" in result
        assert result["deleted"] == 0
        assert result["reclaimed_bytes"] == 0

    def test_scan_respects_newsletter_snooze(self, conn, mock_plex):
        """Items snoozed via the newsletter keep flow (token_used=1, future execute_at) are not re-scheduled."""
        now = datetime.now(timezone.utc)
        future = (now + timedelta(days=25)).isoformat()
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("1001", "Kept Movie", "movie", 1, "1001",
             (now - timedelta(days=90)).isoformat(),
             "/media/movies/Kept", 3_000_000_000),
        )
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "execute_at, token, token_used) VALUES (?, ?, ?, ?, ?, ?)",
            ("1001", "snoozed", now.isoformat(), future, "tok-newsletter", 1),
        )
        conn.commit()

        mock_plex.get_movie_items.return_value = [{
            "plex_rating_key": "1001",
            "title": "Kept Movie",
            "added_at": now - timedelta(days=90),
            "file_path": "/media/movies/Kept",
            "file_size_bytes": 3_000_000_000,
            "poster_path": None,
        }]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 0

    def test_scan_reschedules_expired_snooze(self, conn, mock_plex):
        """Items with an expired snooze (token_used=1, past execute_at) are scheduled for deletion."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(days=5)).isoformat()
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("1002", "Expired Snooze Movie", "movie", 1, "1002",
             (now - timedelta(days=120)).isoformat(),
             "/media/movies/ExpiredSnooze", 5_000_000_000),
        )
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "execute_at, token, token_used) VALUES (?, ?, ?, ?, ?, ?)",
            ("1002", "snoozed", (now - timedelta(days=35)).isoformat(), past, "tok-expired", 1),
        )
        conn.commit()

        mock_plex.get_movie_items.return_value = [{
            "plex_rating_key": "1002",
            "title": "Expired Snooze Movie",
            "added_at": now - timedelta(days=120),
            "file_path": "/media/movies/ExpiredSnooze",
            "file_size_bytes": 5_000_000_000,
            "poster_path": None,
        }]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 1


class TestExecuteDeletions:
    """Tests for the execute_deletions method."""

    def _insert_item(self, conn, item_id, title, file_path="/tmp/fake", file_size=1_000_000):
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, title, "movie", 1, item_id,
             (now - timedelta(days=60)).isoformat(), file_path, file_size),
        )

    def _insert_scheduled_deletion(self, conn, item_id, execute_at):
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, execute_at, token, token_used) "
            "VALUES (?, 'scheduled_deletion', ?, ?, ?, 0)",
            (item_id, datetime.now(timezone.utc).isoformat(), execute_at, f"tok-{item_id}"),
        )
        conn.commit()

    def test_dry_run_does_not_delete_files(self, conn, mock_plex):
        """dry_run=True logs dry_run_skip but does not delete or remove the action row."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(seconds=1)).isoformat()

        self._insert_item(conn, "700", "Dry Run Movie")
        self._insert_scheduled_deletion(conn, "700", past)

        with patch("mediaman.services.storage.delete_path") as mock_delete:
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

        # The scheduled_deletion row must still be there
        row = conn.execute(
            "SELECT * FROM scheduled_actions WHERE media_item_id='700'"
        ).fetchone()
        assert row is not None

        # An audit entry with dry_run_skip should exist
        log = conn.execute(
            "SELECT * FROM audit_log WHERE media_item_id='700'"
        ).fetchone()
        assert log is not None
        assert log["action"] == "dry_run_skip"

    def test_execute_deletes_past_due_items(self, conn, mock_plex, monkeypatch):
        """Items whose execute_at has passed are deleted and the action row removed."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        self._insert_item(conn, "800", "Deletable Movie", file_size=2_000_000)
        self._insert_scheduled_deletion(conn, "800", past)

        with patch("mediaman.scanner.engine.delete_path") as mock_delete:
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

        # scheduled_actions row must be gone
        row = conn.execute(
            "SELECT * FROM scheduled_actions WHERE media_item_id='800'"
        ).fetchone()
        assert row is None

        # audit_log must have a 'deleted' entry
        log = conn.execute(
            "SELECT * FROM audit_log WHERE media_item_id='800'"
        ).fetchone()
        assert log is not None
        assert log["action"] == "deleted"
        assert log["space_reclaimed_bytes"] == 2_000_000

    def test_future_deletions_not_executed(self, conn, mock_plex):
        """Items whose execute_at is in the future are not touched."""
        now = datetime.now(timezone.utc)
        future = (now + timedelta(days=7)).isoformat()

        self._insert_item(conn, "900", "Future Movie")
        self._insert_scheduled_deletion(conn, "900", future)

        with patch("mediaman.scanner.engine.delete_path") as mock_delete:
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

    def test_radarr_unmonitor_called(self, conn, mock_plex, monkeypatch):
        """execute_deletions calls radarr.unmonitor_movie when radarr_id is set."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes, radarr_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("910", "Radarr Movie", "movie", 1, "910",
             (now - timedelta(days=60)).isoformat(), "/tmp/fake", 500_000, 42),
        )
        self._insert_scheduled_deletion(conn, "910", past)

        mock_radarr = MagicMock()

        with patch("mediaman.scanner.engine.delete_path"):
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

    def test_sonarr_unmonitor_called(self, conn, mock_plex, monkeypatch):
        """execute_deletions calls sonarr.unmonitor_season when sonarr_id + season_number set."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes, sonarr_id, season_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("920", "Sonarr Show S1", "season", 1, "920",
             (now - timedelta(days=60)).isoformat(), "/tmp/fake", 800_000, 99, 1),
        )
        self._insert_scheduled_deletion(conn, "920", past)

        mock_sonarr = MagicMock()

        with patch("mediaman.scanner.engine.delete_path"):
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

    def test_arr_failure_does_not_abort_deletion(self, conn, mock_plex, monkeypatch):
        """A crash in radarr.unmonitor_movie does not prevent the item being deleted."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes, radarr_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("930", "Exploding Movie", "movie", 1, "930",
             (now - timedelta(days=60)).isoformat(), "/tmp/fake", 100_000, 7),
        )
        self._insert_scheduled_deletion(conn, "930", past)

        mock_radarr = MagicMock()
        mock_radarr.unmonitor_movie.side_effect = RuntimeError("radarr down")

        with patch("mediaman.scanner.engine.delete_path"):
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
        self, conn, mock_plex, monkeypatch, caplog,
    ):
        """With no roots configured, the scheduled deletion is skipped and left
        intact so a later run (once the admin sets a root) can execute it."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        self._insert_item(conn, "880", "No Roots Movie")
        self._insert_scheduled_deletion(conn, "880", past)

        with patch("mediaman.scanner.engine.delete_path") as mock_delete:
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="test-key",
            )
            with caplog.at_level("ERROR", logger="mediaman"):
                result = engine.execute_deletions()

        # delete_path must never be invoked without an allowlist
        mock_delete.assert_not_called()
        assert result["deleted"] == 0

        # The scheduled row must still be there so a later run can process it
        row = conn.execute(
            "SELECT * FROM scheduled_actions WHERE media_item_id='880'"
        ).fetchone()
        assert row is not None

        # Operator-facing error must be clear and actionable
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "delete_allowed_roots" in messages
        assert "MEDIAMAN_DELETE_ROOTS" in messages

    def test_scanner_continues_after_skip_when_one_item_has_bad_root(
        self, conn, mock_plex, monkeypatch,
    ):
        """If the allowlist is set but a single item's path is outside it,
        only that item is skipped — other deletions still proceed."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        self._insert_item(conn, "881", "Good Root", file_path="/tmp/fake")
        self._insert_scheduled_deletion(conn, "881", past)
        self._insert_item(conn, "882", "Bad Root", file_path="/etc/passwd")
        self._insert_scheduled_deletion(conn, "882", past)

        def fake_delete(path, *, allowed_roots):
            if path == "/etc/passwd":
                raise ValueError("outside allowed roots")

        with patch("mediaman.scanner.engine.delete_path", side_effect=fake_delete):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="test-key",
            )
            result = engine.execute_deletions()

        assert result["deleted"] == 1

    def test_expired_snoozes_are_cleaned_up(self, conn, mock_plex):
        """Snoozed rows with a past execute_at are deleted so items re-enter the pipeline."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(seconds=1)).isoformat()

        self._insert_item(conn, "940", "Snoozed Item")
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, execute_at, token, token_used) "
            "VALUES (?, 'snoozed', ?, ?, ?, 0)",
            ("940", now.isoformat(), past, "tok-snooze-940"),
        )
        conn.commit()

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[],
            library_types={},
            secret_key="test-key",
        )
        engine.execute_deletions()

        row = conn.execute(
            "SELECT * FROM scheduled_actions WHERE media_item_id='940'"
        ).fetchone()
        assert row is None

    def test_future_snoozes_are_preserved(self, conn, mock_plex):
        """Active snoozes (execute_at in the future) are not cleaned up."""
        now = datetime.now(timezone.utc)
        future = (now + timedelta(days=7)).isoformat()

        self._insert_item(conn, "950", "Active Snooze Item")
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, execute_at, token, token_used) "
            "VALUES (?, 'snoozed', ?, ?, ?, 0)",
            ("950", now.isoformat(), future, "tok-snooze-950"),
        )
        conn.commit()

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[],
            library_types={},
            secret_key="test-key",
        )
        engine.execute_deletions()

        row = conn.execute(
            "SELECT * FROM scheduled_actions WHERE media_item_id='950'"
        ).fetchone()
        assert row is not None


class TestShowLevelKeep:
    """Tests for show-level keep rules via kept_shows table."""

    def test_show_rating_key_stored_during_tv_scan(self, conn, mock_plex):
        now = datetime.now(timezone.utc)
        mock_plex.get_show_seasons.return_value = [{
            "plex_rating_key": "600",
            "title": "Test Show",
            "show_title": "Test Show",
            "season_number": 1,
            "added_at": now - timedelta(days=5),
            "file_path": "/media/tv/Test Show/Season 01",
            "file_size_bytes": 5_000_000_000,
            "poster_path": "/library/metadata/600/thumb/1",
            "episode_count": 10,
            "show_rating_key": "599",
        }]

        engine = ScanEngine(
            conn=conn, plex_client=mock_plex,
            library_ids=["2"], library_types={"2": "show"},
            secret_key="test-key",
        )
        engine.run_scan()

        row = conn.execute("SELECT show_rating_key FROM media_items WHERE id='600'").fetchone()
        assert row is not None
        assert row["show_rating_key"] == "599"

    def test_kept_show_skips_all_seasons(self, conn, mock_plex):
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO kept_shows (show_rating_key, show_title, action, created_at) "
            "VALUES (?, ?, 'protected_forever', ?)",
            ("599", "Test Show", now.isoformat()),
        )
        conn.commit()

        mock_plex.get_show_seasons.return_value = [{
            "plex_rating_key": "600",
            "title": "Test Show",
            "show_title": "Test Show",
            "season_number": 1,
            "added_at": now - timedelta(days=90),
            "file_path": "/media/tv/Test Show/Season 01",
            "file_size_bytes": 5_000_000_000,
            "poster_path": None,
            "episode_count": 10,
            "show_rating_key": "599",
        }]
        mock_plex.get_season_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn, plex_client=mock_plex,
            library_ids=["2"], library_types={"2": "show"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 0
        assert result["skipped"] == 1

    def test_expired_show_snooze_allows_scan(self, conn, mock_plex):
        now = datetime.now(timezone.utc)
        past = (now - timedelta(days=1)).isoformat()
        conn.execute(
            "INSERT INTO kept_shows (show_rating_key, show_title, action, execute_at, created_at) "
            "VALUES (?, ?, 'snoozed', ?, ?)",
            ("599", "Test Show", past, now.isoformat()),
        )
        conn.commit()

        mock_plex.get_show_seasons.return_value = [{
            "plex_rating_key": "600",
            "title": "Test Show",
            "show_title": "Test Show",
            "season_number": 1,
            "added_at": now - timedelta(days=90),
            "file_path": "/media/tv/Test Show/Season 01",
            "file_size_bytes": 5_000_000_000,
            "poster_path": None,
            "episode_count": 10,
            "show_rating_key": "599",
        }]
        mock_plex.get_season_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn, plex_client=mock_plex,
            library_ids=["2"], library_types={"2": "show"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 1

        # Expired row should be cleaned up
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='599'").fetchone()
        assert row is None

    def test_naive_plex_datetime_stored_as_correct_utc(self, conn, mock_plex):
        """Plex returns naive datetimes in local time; they must be converted
        to UTC properly, not mislabelled with .replace(tzinfo=UTC).

        Regression test: when the server's local timezone is ahead of UTC,
        .replace(tzinfo=UTC) shifts the stored date into the future, causing
        _days_ago to return '' and hiding the 'Added today' subtitle.
        """
        # Simulate a naive datetime in local time (as PlexAPI returns).
        # Use a fixed POSIX timestamp so the expected UTC value is unambiguous.
        posix_ts = 1744400000  # 2025-04-11 ~19:33 UTC
        naive_local = datetime.fromtimestamp(posix_ts)  # naive, local tz

        mock_plex.get_movie_items.return_value = [{
            "plex_rating_key": "9999",
            "title": "Timezone Test",
            "added_at": naive_local,
            "file_path": "/media/movies/Timezone Test (2025)",
            "file_size_bytes": 1_500_000_000,
            "poster_path": None,
        }]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        engine.sync_library()

        row = conn.execute(
            "SELECT added_at FROM media_items WHERE id='9999'"
        ).fetchone()
        stored = datetime.fromisoformat(row["added_at"])
        expected_utc = datetime.fromtimestamp(posix_ts, tz=timezone.utc)

        # The stored value must represent the same instant as the original
        # POSIX timestamp — not be offset by the local UTC difference.
        assert abs((stored - expected_utc).total_seconds()) < 2
