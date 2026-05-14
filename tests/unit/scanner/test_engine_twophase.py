"""Tests for delete-roots config parsing and two-phase delete / crash recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.scanner.engine import ScanEngine
from mediaman.services.infra import DeletionRefused
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
        insert_media_item(
            conn,
            id=item_id,
            title=f"t-{item_id}",
            plex_rating_key=item_id,
            file_path=file_path,
            file_size_bytes=size,
        )

    def _insert_sched(self, conn, item_id, past, *, status="pending"):
        insert_scheduled_action(
            conn,
            media_item_id=item_id,
            action="scheduled_deletion",
            token=f"tok-{item_id}",
            execute_at=past,
            token_used=False,
            delete_status=status,
        )

    def test_marks_deleting_before_rm_and_deletes_row_after(
        self,
        conn,
        mock_plex,
        monkeypatch,
        freezer,
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

    def test_rollback_on_value_error(self, conn, mock_plex, monkeypatch, freezer):
        """When delete_path refuses, the marker must roll back to pending
        so a later run can retry."""
        now = datetime.now(UTC)
        past = (now - timedelta(seconds=1)).isoformat()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/tmp")
        self._insert_item(conn, "d2", file_path="/etc/passwd")
        self._insert_sched(conn, "d2", past)

        with patch(
            "mediaman.scanner.deletions.delete_path",
            side_effect=DeletionRefused("outside allowed roots"),
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
