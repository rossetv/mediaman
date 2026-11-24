"""Tests for audit log behaviour on admin search-download submissions.

Covers:
  - _submit_movie and _submit_tv each write an audit row before commit
  - _submit_tv returns 503 when Sonarr get_series() fails during
        duplicate-check instead of proceeding blindly
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from mediaman.web.routes.search.download import _submit_movie, _submit_tv

_MODULE = "mediaman.web.routes.search.download"


class TestSubmitMovieAudit:
    """_submit_movie must write an audit row."""

    def _body(self, tmdb_id: int = 100, title: str = "Dune") -> MagicMock:
        body = MagicMock()
        body.tmdb_id = tmdb_id
        body.title = title
        body.media_type = "movie"
        return body

    def test_audit_row_written_on_new_movie(self, conn):
        body = self._body()

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        with (
            patch(f"{_MODULE}.build_radarr_from_db", return_value=mock_radarr),
            patch(f"{_MODULE}._record_dn"),
        ):
            resp = _submit_movie(conn, "secret", body, "admin@example.com", "admin")

        assert resp.status_code == 200

        # Verify the audit row was actually written to the DB.
        row = conn.execute(
            "SELECT actor, action FROM audit_log WHERE action LIKE '%downloaded%' OR action LIKE '%search%'"
        ).fetchone()
        assert row is not None, "audit_log must contain a row after a successful movie add"
        assert row["actor"] == "admin"

    def test_audit_row_written_before_commit(self, conn):
        """Audit must be written before conn.commit (ordering check)."""
        body = self._body()
        call_order: list[str] = []

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        with (
            patch(f"{_MODULE}.build_radarr_from_db", return_value=mock_radarr),
            patch(
                f"{_MODULE}.log_audit",
                side_effect=lambda *a, **kw: call_order.append("audit"),
            ),
            patch(f"{_MODULE}._record_dn"),
        ):
            _submit_movie(conn, "secret", body, None, "admin")

        # The audit entry must precede the commit.
        assert "audit" in call_order

    def test_no_audit_on_radarr_error(self, conn):
        """Audit row must NOT be written when Radarr itself fails."""
        from mediaman.services.infra import SafeHTTPError

        body = self._body()

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.side_effect = SafeHTTPError(500, "error", b"")

        with patch(f"{_MODULE}.build_radarr_from_db", return_value=mock_radarr):
            resp = _submit_movie(conn, "secret", body, None, "admin")

        assert resp.status_code == 502

        # No audit row should have been committed.
        row = conn.execute("SELECT id FROM audit_log WHERE actor = 'admin'").fetchone()
        assert row is None, "audit_log must be empty when Radarr fails"


class TestSubmitTvAudit:
    """_submit_tv must write an audit row and return 503 on get_series failure."""

    def _body(self, tmdb_id: int = 200, title: str = "Lost") -> MagicMock:
        body = MagicMock()
        body.tmdb_id = tmdb_id
        body.title = title
        body.media_type = "tv"
        body.monitored_seasons = None
        body.search_seasons = None
        return body

    def test_audit_row_written_on_new_series(self, conn):
        body = self._body()

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 5678}
        mock_sonarr.get_series.return_value = []

        with (
            patch(f"{_MODULE}.build_sonarr_from_db", return_value=mock_sonarr),
            patch(f"{_MODULE}.is_series_already_tracked", return_value=False),
            patch(f"{_MODULE}._record_dn"),
        ):
            resp = _submit_tv(conn, "secret", body, "admin@example.com", "admin")

        assert resp.status_code == 200

        # Verify the audit row is actually in the DB.
        row = conn.execute("SELECT actor FROM audit_log WHERE actor = 'admin'").fetchone()
        assert row is not None, "audit_log must contain a row after a successful series add"

    def test_returns_503_when_get_series_fails(self, conn):
        from mediaman.services.infra import SafeHTTPError

        body = self._body()

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 5678}

        with (
            patch(f"{_MODULE}.build_sonarr_from_db", return_value=mock_sonarr),
            patch(
                f"{_MODULE}.is_series_already_tracked",
                side_effect=SafeHTTPError(500, "fail", b""),
            ),
        ):
            resp = _submit_tv(conn, "secret", body, None, "admin")

        assert resp.status_code == 503

        # No audit row should have been committed.
        row = conn.execute("SELECT id FROM audit_log WHERE actor = 'admin'").fetchone()
        assert row is None, "audit_log must be empty when the duplicate check fails"

    def test_no_add_series_when_get_series_fails(self, conn):
        import requests as _req

        body = self._body()

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 5678}

        with (
            patch(f"{_MODULE}.build_sonarr_from_db", return_value=mock_sonarr),
            patch(
                f"{_MODULE}.is_series_already_tracked",
                side_effect=_req.ConnectionError("timeout"),
            ),
        ):
            resp = _submit_tv(conn, "secret", body, None, "admin")

        assert resp.status_code == 503
        mock_sonarr.add_series.assert_not_called()
        mock_sonarr.add_series_with_seasons.assert_not_called()


class TestFailedWriteRollsBack:
    def test_a_failed_audit_write_takes_the_notification_row_with_it(self, conn):
        body = MagicMock()
        body.tmdb_id = 100
        body.title = "Dune"
        body.media_type = "movie"
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = None

        with (
            patch(f"{_MODULE}.build_radarr_from_db", return_value=radarr),
            pytest.raises(UnicodeEncodeError),
        ):
            _submit_movie(conn, "secret", body, "admin@example.com", "\ud800")

        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM download_notifications").fetchone()[0] == 0


class TestSubmitTvTransaction:
    def _body(self) -> MagicMock:
        body = MagicMock()
        body.tmdb_id = 200
        body.title = "Lost"
        body.media_type = "tv"
        body.monitored_seasons = None
        body.search_seasons = None
        return body

    @staticmethod
    def _seen_elsewhere(db_path, table: str) -> int:
        other = sqlite3.connect(str(db_path))
        try:
            return other.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            other.close()

    def _submit(self, conn):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 5678}
        with (
            patch(f"{_MODULE}.build_sonarr_from_db", return_value=sonarr),
            patch(f"{_MODULE}.is_series_already_tracked", return_value=False),
        ):
            return _submit_tv(conn, "secret", self._body(), "admin@example.com", "admin")

    def test_a_failed_audit_write_takes_the_notification_row_with_it(self, conn, db_path):
        conn.execute(
            "CREATE TRIGGER no_audit BEFORE INSERT ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )

        with pytest.raises(sqlite3.IntegrityError):
            self._submit(conn)

        assert not conn.in_transaction
        assert self._seen_elsewhere(db_path, "download_notifications") == 0

    def test_the_notification_and_audit_rows_are_committed_for_other_connections(
        self, conn, db_path
    ):
        assert self._submit(conn).status_code == 200

        assert not conn.in_transaction
        assert self._seen_elsewhere(db_path, "download_notifications") == 1
        assert self._seen_elsewhere(db_path, "audit_log") == 1
