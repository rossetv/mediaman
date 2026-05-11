"""Tests for scan engine orchestration."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import finish_scan_run, init_db, is_scan_running, start_scan_run
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
        now = datetime.now(UTC)
        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "100",
                "title": "Old Movie",
                "added_at": now - timedelta(days=60),
                "file_path": "/media/movies/Old Movie (2020)",
                "file_size_bytes": 5_000_000_000,
                "poster_path": "/library/metadata/100/thumb/1",
            }
        ]
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
        now = datetime.now(UTC)
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "100",
                "Protected Movie",
                "movie",
                1,
                "100",
                (now - timedelta(days=60)).isoformat(),
                "/media/movies/Protected",
                5_000_000_000,
            ),
        )
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "token, token_used) VALUES (?, ?, ?, ?, ?)",
            ("100", "protected_forever", now.isoformat(), "tok-123", 0),
        )
        conn.commit()

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "100",
                "title": "Protected Movie",
                "added_at": now - timedelta(days=60),
                "file_path": "/media/movies/Protected",
                "file_size_bytes": 5_000_000_000,
                "poster_path": None,
            }
        ]

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
        now = datetime.now(UTC)
        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "200",
                "title": "New Movie",
                "added_at": now - timedelta(days=5),
                "file_path": "/media/movies/New Movie",
                "file_size_bytes": 3_000_000_000,
                "poster_path": "/library/metadata/200/thumb/1",
            }
        ]

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
        now = datetime.now(UTC)
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "300",
                "Already Scheduled",
                "movie",
                1,
                "300",
                (now - timedelta(days=90)).isoformat(),
                "/media/movies/Scheduled",
                2_000_000_000,
            ),
        )
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "token, token_used) VALUES (?, ?, ?, ?, ?)",
            ("300", "scheduled_deletion", now.isoformat(), "tok-already", 0),
        )
        conn.commit()

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "300",
                "title": "Already Scheduled",
                "added_at": now - timedelta(days=90),
                "file_path": "/media/movies/Scheduled",
                "file_size_bytes": 2_000_000_000,
                "poster_path": None,
            }
        ]

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
        now = datetime.now(UTC)
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "400",
                "Snoozed Movie",
                "movie",
                1,
                "400",
                (now - timedelta(days=120)).isoformat(),
                "/media/movies/Snoozed",
                4_000_000_000,
            ),
        )
        # A snooze that has already expired (token_used=1 means acted upon)
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "token, token_used) VALUES (?, ?, ?, ?, ?)",
            ("400", "snoozed", (now - timedelta(days=60)).isoformat(), "tok-old", 1),
        )
        conn.commit()

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "400",
                "title": "Snoozed Movie",
                "added_at": now - timedelta(days=120),
                "file_path": "/media/movies/Snoozed",
                "file_size_bytes": 4_000_000_000,
                "poster_path": None,
            }
        ]

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
        now = datetime.now(UTC)
        mock_plex.get_show_seasons.return_value = [
            {
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
            }
        ]
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
        now = datetime.now(UTC)
        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "600",
                "title": "Audit Movie",
                "added_at": now - timedelta(days=60),
                "file_path": "/media/movies/Audit",
                "file_size_bytes": 1_000_000_000,
                "poster_path": None,
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        engine.run_scan()

        row = conn.execute("SELECT * FROM audit_log WHERE media_item_id='600'").fetchone()
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
        now = datetime.now(UTC)
        future = (now + timedelta(days=25)).isoformat()
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1001",
                "Kept Movie",
                "movie",
                1,
                "1001",
                (now - timedelta(days=90)).isoformat(),
                "/media/movies/Kept",
                3_000_000_000,
            ),
        )
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "execute_at, token, token_used) VALUES (?, ?, ?, ?, ?, ?)",
            ("1001", "snoozed", now.isoformat(), future, "tok-newsletter", 1),
        )
        conn.commit()

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "1001",
                "title": "Kept Movie",
                "added_at": now - timedelta(days=90),
                "file_path": "/media/movies/Kept",
                "file_size_bytes": 3_000_000_000,
                "poster_path": None,
            }
        ]

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
        now = datetime.now(UTC)
        past = (now - timedelta(days=5)).isoformat()
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1002",
                "Expired Snooze Movie",
                "movie",
                1,
                "1002",
                (now - timedelta(days=120)).isoformat(),
                "/media/movies/ExpiredSnooze",
                5_000_000_000,
            ),
        )
        conn.execute(
            "INSERT INTO scheduled_actions (media_item_id, action, scheduled_at, "
            "execute_at, token, token_used) VALUES (?, ?, ?, ?, ?, ?)",
            ("1002", "snoozed", (now - timedelta(days=35)).isoformat(), past, "tok-expired", 1),
        )
        conn.commit()

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "1002",
                "title": "Expired Snooze Movie",
                "added_at": now - timedelta(days=120),
                "file_path": "/media/movies/ExpiredSnooze",
                "file_size_bytes": 5_000_000_000,
                "poster_path": None,
            }
        ]

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
        now = datetime.now(UTC)
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                title,
                "movie",
                1,
                item_id,
                (now - timedelta(days=60)).isoformat(),
                file_path,
                file_size,
            ),
        )

    def _insert_scheduled_deletion(self, conn, item_id, execute_at):
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, execute_at, token, token_used) "
            "VALUES (?, 'scheduled_deletion', ?, ?, ?, 0)",
            (item_id, datetime.now(UTC).isoformat(), execute_at, f"tok-{item_id}"),
        )
        conn.commit()

    def test_dry_run_does_not_delete_files(self, conn, mock_plex):
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

        # The scheduled_deletion row must still be there
        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='700'").fetchone()
        assert row is not None

        # An audit entry with dry_run_skip should exist
        log = conn.execute("SELECT * FROM audit_log WHERE media_item_id='700'").fetchone()
        assert log is not None
        assert log["action"] == "dry_run_skip"

    def test_execute_deletes_past_due_items(self, conn, mock_plex, monkeypatch):
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

        # scheduled_actions row must be gone
        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='800'").fetchone()
        assert row is None

        # audit_log must have a 'deleted' entry
        log = conn.execute("SELECT * FROM audit_log WHERE media_item_id='800'").fetchone()
        assert log is not None
        assert log["action"] == "deleted"
        assert log["space_reclaimed_bytes"] == 2_000_000

    def test_future_deletions_not_executed(self, conn, mock_plex):
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

    def test_radarr_unmonitor_called(self, conn, mock_plex, monkeypatch):
        """execute_deletions calls radarr.unmonitor_movie when radarr_id is set."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes, radarr_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "910",
                "Radarr Movie",
                "movie",
                1,
                "910",
                (now - timedelta(days=60)).isoformat(),
                "/tmp/fake",
                500_000,
                42,
            ),
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

    def test_sonarr_unmonitor_called(self, conn, mock_plex, monkeypatch):
        """execute_deletions calls sonarr.unmonitor_season when sonarr_id + season_number set."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes, sonarr_id, season_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "920",
                "Sonarr Show S1",
                "season",
                1,
                "920",
                (now - timedelta(days=60)).isoformat(),
                "/tmp/fake",
                800_000,
                99,
                1,
            ),
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

    def test_arr_failure_does_not_abort_deletion(self, conn, mock_plex, monkeypatch):
        """A crash in radarr.unmonitor_movie does not prevent the item being deleted."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes, radarr_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "930",
                "Exploding Movie",
                "movie",
                1,
                "930",
                (now - timedelta(days=60)).isoformat(),
                "/tmp/fake",
                100_000,
                7,
            ),
        )
        self._insert_scheduled_deletion(conn, "930", past)

        mock_radarr = MagicMock()
        mock_radarr.unmonitor_movie.side_effect = RuntimeError("radarr down")

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

        # delete_path must never be invoked without an allowlist
        mock_delete.assert_not_called()
        assert result["deleted"] == 0

        # The scheduled row must still be there so a later run can process it
        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='880'").fetchone()
        assert row is not None

        # Operator-facing error must be clear and actionable
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "delete_allowed_roots" in messages
        assert "MEDIAMAN_DELETE_ROOTS" in messages

    def test_scanner_continues_after_skip_when_one_item_has_bad_root(
        self,
        conn,
        mock_plex,
        monkeypatch,
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

    def test_expired_snoozes_are_cleaned_up(self, conn, mock_plex):
        """Snoozed rows with a past execute_at are deleted so items re-enter the pipeline."""
        now = datetime.now(UTC)
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

        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='940'").fetchone()
        assert row is None

    def test_future_snoozes_are_preserved(self, conn, mock_plex):
        """Active snoozes (execute_at in the future) are not cleaned up."""
        now = datetime.now(UTC)
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

        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='950'").fetchone()
        assert row is not None


class TestShowLevelKeep:
    """Tests for show-level keep rules via kept_shows table."""

    def test_show_rating_key_stored_during_tv_scan(self, conn, mock_plex):
        now = datetime.now(UTC)
        mock_plex.get_show_seasons.return_value = [
            {
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
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
        )
        engine.run_scan()

        row = conn.execute("SELECT show_rating_key FROM media_items WHERE id='600'").fetchone()
        assert row is not None
        assert row["show_rating_key"] == "599"

    def test_kept_show_skips_all_seasons(self, conn, mock_plex):
        now = datetime.now(UTC)
        conn.execute(
            "INSERT INTO kept_shows (show_rating_key, show_title, action, created_at) "
            "VALUES (?, ?, 'protected_forever', ?)",
            ("599", "Test Show", now.isoformat()),
        )
        conn.commit()

        mock_plex.get_show_seasons.return_value = [
            {
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
            }
        ]
        mock_plex.get_season_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 0
        assert result["skipped"] == 1

    def test_expired_show_snooze_allows_scan(self, conn, mock_plex):
        now = datetime.now(UTC)
        past = (now - timedelta(days=1)).isoformat()
        conn.execute(
            "INSERT INTO kept_shows (show_rating_key, show_title, action, execute_at, created_at) "
            "VALUES (?, ?, 'snoozed', ?, ?)",
            ("599", "Test Show", past, now.isoformat()),
        )
        conn.commit()

        mock_plex.get_show_seasons.return_value = [
            {
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
            }
        ]
        mock_plex.get_season_watch_history.return_value = []

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["2"],
            library_types={"2": "show"},
            secret_key="test-key",
        )
        result = engine.run_scan()
        assert result["scheduled"] == 1

        # Expired row should be cleaned up
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key='599'").fetchone()
        assert row is None

    def test_plex_datetime_stored_as_correct_utc(self, conn, mock_plex):
        """Plex datetimes must round-trip to UTC without offset drift.

        The Plex client (`media_meta/plex.py:_to_utc`) now promotes
        plexapi's naive local-time datetimes to tz-aware UTC at the
        boundary, so by the time `engine._resolve_added_at` sees them
        they are already aware. This test verifies the engine stores
        the aware UTC value unchanged (no `.replace(tzinfo=UTC)`
        mislabelling, no double-conversion).
        """
        # Use a fixed POSIX timestamp so the expected UTC value is
        # unambiguous. Pass it through as a tz-aware UTC datetime —
        # which is what the production Plex client now produces.
        posix_ts = 1744400000  # 2025-04-11 ~19:33 UTC
        aware_utc = datetime.fromtimestamp(posix_ts, tz=UTC)

        mock_plex.get_movie_items.return_value = [
            {
                "plex_rating_key": "9999",
                "title": "Timezone Test",
                "added_at": aware_utc,
                "file_path": "/media/movies/Timezone Test (2025)",
                "file_size_bytes": 1_500_000_000,
                "poster_path": None,
            }
        ]

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["1"],
            library_types={"1": "movie"},
            secret_key="test-key",
        )
        engine.sync_library()

        row = conn.execute("SELECT added_at FROM media_items WHERE id='9999'").fetchone()
        stored = datetime.fromisoformat(row["added_at"])
        expected_utc = datetime.fromtimestamp(posix_ts, tz=UTC)

        # The stored value must represent the same instant as the original
        # POSIX timestamp — not be offset by the local UTC difference.
        assert abs((stored - expected_utc).total_seconds()) < 2


class TestDeleteRootsSeparator:
    """C23: MEDIAMAN_DELETE_ROOTS must accept both ':' and ',' separators
    with ':' being canonical and ',' deprecated."""

    def _engine(self, conn, mock_plex):
        return ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[],
            library_types={},
            secret_key="k",
        )

    def test_colon_separator_parses(self, conn, mock_plex, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/a:/b:/c")
        roots = self._engine(conn, mock_plex)._load_delete_allowed_roots()
        assert roots == ["/a", "/b", "/c"]

    def test_comma_separator_parses_with_deprecation_warning(
        self,
        conn,
        mock_plex,
        monkeypatch,
        caplog,
    ):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/a,/b,/c")
        with caplog.at_level("WARNING", logger="mediaman"):
            roots = self._engine(conn, mock_plex)._load_delete_allowed_roots()
        assert roots == ["/a", "/b", "/c"]
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "deprecated" in msgs.lower()

    def test_mixed_separators_errors_but_still_parses(
        self,
        conn,
        mock_plex,
        monkeypatch,
        caplog,
    ):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/a:/b,/c")
        with caplog.at_level("ERROR", logger="mediaman"):
            roots = self._engine(conn, mock_plex)._load_delete_allowed_roots()
        assert roots == ["/a", "/b", "/c"]
        assert any("both" in r.getMessage().lower() for r in caplog.records)

    def test_empty_value_returns_empty_and_logs_error(
        self,
        conn,
        mock_plex,
        monkeypatch,
        caplog,
    ):
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        with caplog.at_level("ERROR", logger="mediaman"):
            roots = self._engine(conn, mock_plex)._load_delete_allowed_roots()
        assert roots == []
        assert any("not configured" in r.getMessage() for r in caplog.records)


class TestTwoPhaseDelete:
    """C30: deletions must mark a 'deleting' status before the rm so a
    crash mid-way can be recovered by _recover_stuck_deletions."""

    def _insert_item(self, conn, item_id, file_path="/tmp/fake", size=1_000_000):
        now = datetime.now(UTC)
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES (?, ?, 'movie', 1, ?, ?, ?, ?)",
            (
                item_id,
                f"t-{item_id}",
                item_id,
                (now - timedelta(days=60)).isoformat(),
                file_path,
                size,
            ),
        )

    def _insert_sched(self, conn, item_id, past, *, status="pending"):
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, execute_at, token, "
            "token_used, delete_status) "
            "VALUES (?, 'scheduled_deletion', ?, ?, ?, 0, ?)",
            (item_id, datetime.now(UTC).isoformat(), past, f"tok-{item_id}", status),
        )
        conn.commit()

    def test_marks_deleting_before_rm_and_deletes_row_after(
        self,
        conn,
        mock_plex,
        monkeypatch,
    ):
        """Happy path: row flips through 'deleting' then is removed."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        self._insert_item(conn, "d1")
        self._insert_sched(conn, "d1", past)

        seen: dict[str, str | None] = {}

        def fake_delete(path, *, allowed_roots):
            row = conn.execute(
                "SELECT delete_status FROM scheduled_actions WHERE media_item_id='d1'"
            ).fetchone()
            seen["status_during_rm"] = row["delete_status"]

        with patch("mediaman.scanner.deletions.delete_path", side_effect=fake_delete):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="k",
            )
            result = engine.execute_deletions()

        assert seen["status_during_rm"] == "deleting"
        assert result["deleted"] == 1
        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='d1'").fetchone()
        assert row is None

    def test_rollback_on_value_error(self, conn, mock_plex, monkeypatch):
        """When delete_path refuses, the marker must roll back to pending
        so a later run can retry."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        self._insert_item(conn, "d2", file_path="/etc/passwd")
        self._insert_sched(conn, "d2", past)

        with patch(
            "mediaman.scanner.deletions.delete_path",
            side_effect=ValueError("outside allowed roots"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="k",
            )
            engine.execute_deletions()

        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='d2'"
        ).fetchone()
        assert row is not None
        assert row["delete_status"] == "pending"

    def test_recover_file_still_present_resets_to_pending(
        self,
        conn,
        mock_plex,
        tmp_path,
    ):
        """Recovery: a 'deleting' row whose file is still on disk is
        reset to 'pending' so the next run retries it."""
        from mediaman.scanner.deletions import _recover_stuck_deletions

        live = tmp_path / "live.mkv"
        live.write_bytes(b"x")
        self._insert_item(conn, "r1", file_path=str(live))
        self._insert_sched(
            conn,
            "r1",
            "2026-01-01T00:00:00+00:00",
            status="deleting",
        )

        _recover_stuck_deletions(conn)

        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='r1'"
        ).fetchone()
        assert row["delete_status"] == "pending"

    def test_recover_file_absent_completes_cleanup(self, conn, mock_plex):
        """Recovery: a 'deleting' row whose file is already gone gets
        its audit entry written and the row removed."""
        from mediaman.scanner.deletions import _recover_stuck_deletions

        self._insert_item(conn, "r2", file_path="/definitely/not/here/x.mkv")
        self._insert_sched(
            conn,
            "r2",
            "2026-01-01T00:00:00+00:00",
            status="deleting",
        )

        _recover_stuck_deletions(conn)

        row = conn.execute("SELECT * FROM scheduled_actions WHERE media_item_id='r2'").fetchone()
        assert row is None
        audit = conn.execute("SELECT action FROM audit_log WHERE media_item_id='r2'").fetchone()
        assert audit is not None
        assert audit["action"] == "deleted"


class TestOrphanGuard:
    """C31: a scan returning zero (or near-zero) items must not be
    trusted as authoritative — refuse orphan removal and log why."""

    def _populate_items(self, conn, lib_id, n):
        for i in range(n):
            conn.execute(
                "INSERT INTO media_items (id, title, media_type, "
                "plex_library_id, plex_rating_key, added_at, file_path, "
                "file_size_bytes) VALUES (?, ?, 'movie', ?, ?, ?, ?, ?)",
                (
                    f"item-{lib_id}-{i}",
                    f"t-{i}",
                    lib_id,
                    f"item-{lib_id}-{i}",
                    "2026-01-01",
                    f"/media/{i}",
                    1,
                ),
            )
        conn.commit()

    def test_empty_scan_against_populated_lib_refuses_orphan_removal(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        self._populate_items(conn, 7, 20)
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["7"],
            library_types={"7": "movie"},
            secret_key="k",
        )
        with caplog.at_level("WARNING", logger="mediaman"):
            removed = engine._remove_orphaned_items(
                seen_keys=set(),
                scanned_libs={7},
            )
        assert removed == 0
        # DB untouched — all 20 items still present.
        assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 20
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "below_min_items" in msgs

    def test_huge_drop_triggers_ratio_guard(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        self._populate_items(conn, 8, 200)
        # Only 5 items "found" — that's above the 5-item floor but below
        # the 10 % ratio floor (200 * 0.10 = 20).
        seen = {f"item-8-{i}" for i in range(5)}
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["8"],
            library_types={"8": "movie"},
            secret_key="k",
        )
        with caplog.at_level("WARNING", logger="mediaman"):
            removed = engine._remove_orphaned_items(
                seen_keys=seen,
                scanned_libs={8},
            )
        assert removed == 0
        assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 200
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "below_ratio" in msgs

    def test_normal_small_drop_still_removes_orphans(self, conn, mock_plex):
        """A modest drop (e.g. one item removed from a 30-item library)
        must still trigger orphan cleanup — guard only catches collapse."""
        self._populate_items(conn, 9, 30)
        seen = {f"item-9-{i}" for i in range(30) if i != 5}
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["9"],
            library_types={"9": "movie"},
            secret_key="k",
        )
        removed = engine._remove_orphaned_items(
            seen_keys=seen,
            scanned_libs={9},
        )
        assert removed == 1
        assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 29

    def test_fresh_db_with_tiny_scan_is_allowed(self, conn, mock_plex):
        """If the previous count was zero / tiny (genuine first run), the
        min-items floor must not block first-time orphan cleanup."""
        # No prior items at all → previous_count == 0 → guard inactive.
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=["10"],
            library_types={"10": "movie"},
            secret_key="k",
        )
        removed = engine._remove_orphaned_items(
            seen_keys={"x"},
            scanned_libs={10},
        )
        assert removed == 0  # nothing to remove, but not blocked either


class TestConcurrentScanGuard:
    """H60: manual and cron scans cannot both run simultaneously.

    The DB-backed ``scan_runs`` table is the single concurrency gate.
    ``start_scan_run`` uses ``BEGIN IMMEDIATE`` so only one caller
    wins the lock; the second gets ``None`` back and must abort.
    """

    def test_concurrent_manual_and_cron_does_not_double_fire(self, conn):
        """Simulates a manual scan already running when the cron fires.

        Only one scan run should be active at a time. The second call to
        ``start_scan_run`` must return ``None``, indicating the cron path
        should skip execution.
        """
        # Simulate the manual scan acquiring the lock first.
        run_id = start_scan_run(conn)
        assert run_id is not None, "First (manual) caller must acquire the lock"
        assert is_scan_running(conn), "Scan should be marked running after start"

        # Simulate the cron path arriving while the manual scan is active.
        cron_run_id = start_scan_run(conn)
        assert cron_run_id is None, (
            "Second (cron) caller must receive None — another scan is already running"
        )

        # Clean up: finish the manual scan run.
        finish_scan_run(conn, run_id, "done")
        assert not is_scan_running(conn), "Scan should no longer be running after finish"

    def test_second_scan_can_start_after_first_finishes(self, conn):
        """After the first scan completes, a new one can acquire the lock."""
        run_id_1 = start_scan_run(conn)
        assert run_id_1 is not None
        finish_scan_run(conn, run_id_1, "done")

        run_id_2 = start_scan_run(conn)
        assert run_id_2 is not None, "New scan must be startable after the previous one finished"
        assert run_id_2 != run_id_1
        finish_scan_run(conn, run_id_2, "done")


class TestRunScanDryRun:
    """D05 finding 1: dry_run must skip *every* mutating side-effect of a
    full scan, not just the on-disk rm.
    """

    def _stale_movie(self, key="100", days_old=60):
        now = datetime.now(UTC)
        return {
            "plex_rating_key": key,
            "title": f"Stale Movie {key}",
            "added_at": now - timedelta(days=days_old),
            "file_path": f"/media/movies/Stale {key}",
            "file_size_bytes": 1_000_000_000,
            "poster_path": None,
        }

    def test_dry_run_does_not_write_schedule_deletion(self, conn, mock_plex):
        """A stale movie that would normally schedule deletion must NOT
        write a ``scheduled_actions`` row when ``dry_run=True``.
        """
        mock_plex.get_movie_items.return_value = [self._stale_movie("100")]
        mock_plex.get_watch_history.return_value = []

        with (
            patch("mediaman.scanner._post_scan._send_newsletter") as mock_news,
            patch("mediaman.scanner._post_scan._refresh_recommendations") as mock_recs,
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["1"],
                library_types={"1": "movie"},
                secret_key="k",
                min_age_days=30,
                inactivity_days=30,
                dry_run=True,
            )
            result = engine.run_scan()

        assert result["scheduled"] == 1, "summary still reports the would-be schedule"
        # No scheduled_actions row written.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scheduled_actions WHERE action='scheduled_deletion'"
            ).fetchone()[0]
            == 0
        )
        # No audit_log entry from schedule_deletion either.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='scheduled_deletion'"
            ).fetchone()[0]
            == 0
        )
        # Newsletter and recommendations refresh must be skipped.
        mock_news.assert_not_called()
        mock_recs.assert_not_called()

    def test_dry_run_does_not_remove_orphans(self, conn, mock_plex):
        """Pre-existing media_items that are no longer in Plex must NOT
        be deleted in dry_run mode.
        """
        # Seed 50 prior items so the orphan-guard ratio check would
        # otherwise pass once Plex returns nothing.
        for i in range(50):
            conn.execute(
                "INSERT INTO media_items (id, title, media_type, plex_library_id, "
                "plex_rating_key, added_at, file_path, file_size_bytes) "
                "VALUES (?, ?, 'movie', 1, ?, ?, ?, ?)",
                (
                    f"orphan-{i}",
                    f"Title {i}",
                    f"orphan-{i}",
                    "2026-01-01",
                    f"/media/{i}",
                    1,
                ),
            )
        conn.commit()

        # Plex returns one current item — without the dry_run guard,
        # the other 49 would be eligible for orphan removal.
        mock_plex.get_movie_items.return_value = [self._stale_movie("orphan-0", days_old=5)]
        mock_plex.get_watch_history.return_value = []

        with (
            patch("mediaman.scanner._post_scan._send_newsletter"),
            patch("mediaman.scanner._post_scan._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["1"],
                library_types={"1": "movie"},
                secret_key="k",
                dry_run=True,
            )
            result = engine.run_scan()

        assert result["removed"] == 0
        # Original 50 items still present (the upsert may have updated
        # one row to match the Plex item, but no DELETE ran).
        assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 50

    def test_dry_run_does_not_send_newsletter(self, conn, mock_plex):
        """The mailer must not be invoked in dry_run mode (D05 finding 1)."""
        with (
            patch("mediaman.scanner._post_scan._send_newsletter") as mock_news,
            patch("mediaman.scanner._post_scan._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="k",
                dry_run=True,
            )
            engine.run_scan()

        mock_news.assert_not_called()

    def test_dry_run_does_not_clean_up_expired_snoozes(self, conn, mock_plex):
        """An expired snooze must not be deleted in dry_run mode —
        cleanup is a write that ``run_scan`` (via ``execute_deletions``)
        would normally perform but must be suppressed under a true
        preview (D05 finding 10).
        """
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()
        # Seed a media item + expired snooze.
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES ('m1', 'Snoozed', 'movie', 1, 'm1', ?, '/m1', 0)",
            (now.isoformat(),),
        )
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, execute_at, token, token_used) "
            "VALUES ('m1', 'snoozed', ?, ?, 'tok-snz', 0)",
            (now.isoformat(), past),
        )
        conn.commit()

        with (
            patch("mediaman.scanner._post_scan._send_newsletter"),
            patch("mediaman.scanner._post_scan._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=[],
                library_types={},
                secret_key="k",
                dry_run=True,
            )
            engine.run_scan()

        # The expired snooze row must still exist.
        row = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id='m1' AND action='snoozed'"
        ).fetchone()
        assert row is not None


class TestResolveAddedAt:
    """D05 findings 2 + 3: ``_resolve_added_at`` must prefer Arr date,
    then Plex ``added_at``, and only fall back to ``updated_at`` as a
    last resort. An unparseable Arr date must fall through, not be
    substituted with ``now()``.
    """

    def test_prefers_added_at_over_updated_at(self, conn, mock_plex):
        """When Plex ``updated_at`` was reset (e.g. subtitle download)
        but ``added_at`` is from years ago, eligibility must be measured
        against ``added_at``, not the recent metadata-refresh marker.
        """
        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[],
            library_types={},
            secret_key="k",
        )
        old_added = datetime.now(UTC) - timedelta(days=400)
        recent_updated = datetime.now(UTC) - timedelta(hours=1)
        item = {
            "file_path": "/media/movies/Foo",
            "added_at": old_added,
            "updated_at": recent_updated,
        }
        resolved = engine._resolve_added_at(item)
        # Should be old_added (not recent_updated)
        assert abs((resolved - old_added).total_seconds()) < 2

    def test_unparseable_arr_date_falls_through_to_added_at(self, conn, mock_plex):
        """A bad Arr cache value used to be silently replaced by
        ``datetime.now(UTC)`` and gave the item permanent protection.
        It must now fall through to ``added_at`` so eligibility is
        evaluated normally (D05 finding 3).
        """
        from mediaman.scanner.arr_dates import normalise_path

        engine = ScanEngine(
            conn=conn,
            plex_client=mock_plex,
            library_ids=[],
            library_types={},
            secret_key="k",
        )
        # Pre-populate the cache so ensure_loaded() is a no-op and
        # never fires Radarr/Sonarr fetches.
        bad_path = "/media/movies/Bar"
        engine._arr_cache._dates = {normalise_path(bad_path): "not-a-date"}  # type: ignore[attr-defined]
        engine._arr_cache._loaded = True  # type: ignore[attr-defined]
        old_added = datetime.now(UTC) - timedelta(days=400)
        item = {"file_path": bad_path, "added_at": old_added}
        resolved = engine._resolve_added_at(item)
        assert abs((resolved - old_added).total_seconds()) < 2


class TestPerLibraryOrphanGuard:
    """D05 finding 7: orphan-removal numerical safeguards must evaluate
    each library independently. A library that returns 0 items must not
    be wiped just because a sibling library returned 1000.
    """

    def _populate_items(self, conn, lib_id, n):
        for i in range(n):
            conn.execute(
                "INSERT INTO media_items (id, title, media_type, "
                "plex_library_id, plex_rating_key, added_at, file_path, "
                "file_size_bytes) VALUES (?, ?, 'movie', ?, ?, ?, ?, ?)",
                (
                    f"item-{lib_id}-{i}",
                    f"t-{i}",
                    lib_id,
                    f"item-{lib_id}-{i}",
                    "2026-01-01",
                    f"/media/{i}",
                    1,
                ),
            )
        conn.commit()

    def test_empty_library_does_not_wipe_when_sibling_full(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        """Library 7 had 50 items, library 8 had 0 returned by Plex,
        library 9 returned 1000. Pre-fix the union (1000+0=1000 > 0.10
        of 50+1000=1050 = 105) passed, so library 8's 50 items were
        wiped. Per-library evaluation must protect them.
        """
        # Library 7 — 50 items already in DB; Plex returns same 50.
        self._populate_items(conn, 7, 50)
        # Library 8 — 50 items already in DB but Plex returns nothing
        # (auth hiccup scoped to one section).
        self._populate_items(conn, 8, 50)
        # Library 9 — no items in DB; Plex returns 1000 (irrelevant to
        # the safeguard but realistic).

        # Plex now returns: lib 7 = 50 of its items, lib 8 = 0, lib 9 = 1000.
        def get_movies(lib_id):
            if lib_id == "7":
                return [
                    {
                        "plex_rating_key": f"item-7-{i}",
                        "title": f"t-{i}",
                        "added_at": datetime.now(UTC) - timedelta(days=5),
                        "file_path": f"/media/{i}",
                        "file_size_bytes": 1,
                        "poster_path": None,
                    }
                    for i in range(50)
                ]
            if lib_id == "8":
                return []  # auth hiccup — should be skipped/preserved
            if lib_id == "9":
                return [
                    {
                        "plex_rating_key": f"new-9-{i}",
                        "title": f"new-{i}",
                        "added_at": datetime.now(UTC) - timedelta(days=5),
                        "file_path": f"/media/new-{i}",
                        "file_size_bytes": 1,
                        "poster_path": None,
                    }
                    for i in range(1000)
                ]
            return []

        mock_plex.get_movie_items.side_effect = get_movies

        with (
            patch("mediaman.scanner._post_scan._send_newsletter"),
            patch("mediaman.scanner._post_scan._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["7", "8", "9"],
                library_types={"7": "movie", "8": "movie", "9": "movie"},
                secret_key="k",
                min_age_days=999_999,  # make sure nothing is scheduled
            )
            with caplog.at_level("WARNING", logger="mediaman"):
                engine.run_scan()

        # Library 8's items must survive.
        assert (
            conn.execute("SELECT COUNT(*) FROM media_items WHERE plex_library_id=8").fetchone()[0]
            == 50
        )
        # And the per-library guard logged its decision.
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "below_min_items" in msgs


class TestUnknownLibraryType:
    """D05 finding 6: a library with no type mapping must be skipped
    loudly, not scanned as 'movie' by default.
    """

    def test_unknown_library_type_is_skipped_with_warning(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        with (
            patch("mediaman.scanner._post_scan._send_newsletter"),
            patch("mediaman.scanner._post_scan._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["1"],
                library_types={},  # no mapping for "1"
                secret_key="k",
            )
            with caplog.at_level("WARNING", logger="mediaman"):
                result = engine.run_scan()

        assert result["scanned"] == 0
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "unknown_library_type" in msgs

    def test_music_library_type_is_skipped_with_warning(
        self,
        conn,
        mock_plex,
        caplog,
    ):
        with (
            patch("mediaman.scanner._post_scan._send_newsletter"),
            patch("mediaman.scanner._post_scan._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=mock_plex,
                library_ids=["3"],
                library_types={"3": "music"},
                secret_key="k",
            )
            with caplog.at_level("WARNING", logger="mediaman"):
                result = engine.run_scan()

        assert result["scanned"] == 0
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "unsupported_library_type" in msgs
