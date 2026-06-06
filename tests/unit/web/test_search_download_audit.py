"""Tests for audit log behaviour on admin search-download submissions.

Covers:
  - B1: _submit_movie and _submit_tv each write an audit row before commit
  - H7: _submit_tv returns 503 when Sonarr get_series() fails during
        duplicate-check instead of proceeding blindly
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mediaman.web.routes.search.download import _submit_movie, _submit_tv

_MODULE = "mediaman.web.routes.search.download"


class TestSubmitMovieAudit:
    """_submit_movie must write an audit row (B1)."""

    def _body(self, tmdb_id: int = 100, title: str = "Dune") -> MagicMock:
        body = MagicMock()
        body.tmdb_id = tmdb_id
        body.title = title
        body.media_type = "movie"
        return body

    def test_audit_row_written_on_new_movie(self):
        """Audit row is written before commit when a new movie is added."""
        body = self._body()
        conn = MagicMock()
        audit_calls: list[tuple] = []

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        with (
            patch(f"{_MODULE}.build_radarr_from_db", return_value=mock_radarr),
            patch(f"{_MODULE}.log_audit", side_effect=lambda *a, **kw: audit_calls.append((a, kw))),
            patch(f"{_MODULE}._record_dn"),
        ):
            resp = _submit_movie(conn, "secret", body, "admin@example.com", "admin")

        assert resp.status_code == 200
        assert len(audit_calls) == 1
        _, kw = audit_calls[0]
        assert kw["actor"] == "admin"
        assert "downloaded" in audit_calls[0][0]
        # Commit must happen after the audit write.
        conn.commit.assert_called_once()

    def test_audit_row_written_before_commit(self):
        """Audit must be written before conn.commit (ordering check)."""
        body = self._body()
        conn = MagicMock()
        call_order: list[str] = []

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.return_value = None

        with (
            patch(f"{_MODULE}.build_radarr_from_db", return_value=mock_radarr),
            patch(f"{_MODULE}.log_audit", side_effect=lambda *a, **kw: call_order.append("audit")),
            patch(f"{_MODULE}._record_dn"),
        ):
            conn.commit.side_effect = lambda: call_order.append("commit")
            _submit_movie(conn, "secret", body, None, "admin")

        assert call_order.index("audit") < call_order.index("commit")

    def test_no_audit_on_radarr_error(self):
        """Audit row must NOT be written when Radarr itself fails."""
        from mediaman.services.infra import SafeHTTPError

        body = self._body()
        conn = MagicMock()
        audit_calls: list = []

        mock_radarr = MagicMock()
        mock_radarr.get_movie_by_tmdb.side_effect = SafeHTTPError(500, "error", b"")

        with (
            patch(f"{_MODULE}.build_radarr_from_db", return_value=mock_radarr),
            patch(f"{_MODULE}.log_audit", side_effect=lambda *a, **kw: audit_calls.append(1)),
        ):
            resp = _submit_movie(conn, "secret", body, None, "admin")

        assert resp.status_code == 502
        assert audit_calls == []
        conn.commit.assert_not_called()


class TestSubmitTvAudit:
    """_submit_tv must write an audit row and return 503 on get_series failure (B1/H7)."""

    def _body(self, tmdb_id: int = 200, title: str = "Lost") -> MagicMock:
        body = MagicMock()
        body.tmdb_id = tmdb_id
        body.title = title
        body.media_type = "tv"
        body.monitored_seasons = None
        body.search_seasons = None
        return body

    def test_audit_row_written_on_new_series(self):
        """Audit row is written before commit when a new series is added."""
        body = self._body()
        conn = MagicMock()
        audit_calls: list[tuple] = []

        mock_sonarr = MagicMock()
        # lookup_series_by_tmdb returns a series with tvdbId
        mock_sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 5678}
        mock_sonarr.get_series.return_value = []

        with (
            patch(f"{_MODULE}.build_sonarr_from_db", return_value=mock_sonarr),
            patch(
                f"{_MODULE}.is_series_already_tracked",
                return_value=False,
            ),
            patch(
                f"{_MODULE}.log_audit",
                side_effect=lambda *a, **kw: audit_calls.append((a, kw)),
            ),
            patch(f"{_MODULE}._record_dn"),
        ):
            resp = _submit_tv(conn, "secret", body, "admin@example.com", "admin")

        assert resp.status_code == 200
        assert len(audit_calls) == 1
        _, kw = audit_calls[0]
        assert kw["actor"] == "admin"
        conn.commit.assert_called_once()

    def test_returns_503_when_get_series_fails(self):
        """H7: returns 503 (not 409 or 200) when Sonarr get_series fails."""
        from mediaman.services.infra import SafeHTTPError

        body = self._body()
        conn = MagicMock()

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
        conn.commit.assert_not_called()

    def test_no_add_series_when_get_series_fails(self):
        """H7: add_series must NOT be called when the duplicate check fails."""
        import requests as _req

        body = self._body()
        conn = MagicMock()

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
