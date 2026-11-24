"""Search and Recommended requests record who made them in the audit text."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mediaman.web.routes.recommended.api import reset_download_action_limiter
from mediaman.web.routes.search.download import _submit_movie, _submit_tv
from tests.helpers.factories import insert_suggestion
from tests.unit.web.test_recommended_api import _make_rec_app

_SEARCH = "mediaman.web.routes.search.download"
_REC = "mediaman.web.routes.recommended.api"


def _audit(conn):
    return conn.execute("SELECT action, detail, actor FROM audit_log").fetchall()


def _body(media_type: str) -> MagicMock:
    body = MagicMock()
    body.tmdb_id = 100
    body.title = "Dune"
    body.media_type = media_type
    return body


@pytest.fixture
def _reset():
    reset_download_action_limiter()


def test_search_movie_detail_names_the_requester(conn):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    with (
        patch(f"{_SEARCH}.build_radarr_from_db", return_value=radarr),
        patch(f"{_SEARCH}._record_dn"),
    ):
        _submit_movie(conn, "secret", _body("movie"), None, "alice")
    (row,) = _audit(conn)
    assert row["detail"] == "Added 'Dune' to Radarr by alice"
    assert row["actor"] == "alice"


def test_search_tv_detail_names_the_requester(conn):
    sonarr = MagicMock()
    sonarr.get_series.return_value = []
    sonarr.lookup_by_tmdb_id.return_value = [{"tmdbId": 100, "tvdbId": 5}]
    body = _body("tv")
    body.seasons = None
    with (
        patch(f"{_SEARCH}.build_sonarr_from_db", return_value=sonarr),
        patch(f"{_SEARCH}._record_dn"),
        patch(f"{_SEARCH}.is_series_already_tracked", return_value=False),
        patch(f"{_SEARCH}._resolve_tvdb_id", return_value=(5, None)),
    ):
        resp = _submit_tv(conn, "secret", body, None, "alice")
    assert resp.status_code == 200
    (row,) = _audit(conn)
    assert row["detail"] == "Added 'Dune' to Sonarr by alice"
    assert row["actor"] == "alice"


def test_recommended_movie_writes_audit_row_naming_the_requester(conn, _reset):
    sid = insert_suggestion(conn, title="Inception", tmdb_id=27205, media_type="movie")
    client = _make_rec_app(conn)
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    with patch(f"{_REC}.build_radarr_from_db", return_value=radarr):
        assert client.post(f"/api/recommended/{sid}/download").status_code == 200
    (row,) = _audit(conn)
    assert (row["action"], row["actor"]) == ("downloaded", "admin")
    assert row["detail"] == "Added 'Inception' to Radarr by admin"


def test_recommended_tv_writes_audit_row_naming_the_requester(conn, _reset):
    sid = insert_suggestion(conn, title="Severance", tmdb_id=136315, media_type="tv")
    client = _make_rec_app(conn)
    sonarr = MagicMock()
    sonarr.lookup_by_tmdb_id.return_value = [{"tmdbId": 136315, "tvdbId": 999}]
    with patch(f"{_REC}.build_sonarr_from_db", return_value=sonarr):
        assert client.post(f"/api/recommended/{sid}/download").status_code == 200
    (row,) = _audit(conn)
    assert row["detail"] == "Added 'Severance' to Sonarr by admin"


def test_recommended_failure_writes_no_audit_row(conn, _reset):
    sid = insert_suggestion(conn, title="Inception", tmdb_id=27205, media_type="movie")
    client = _make_rec_app(conn)
    with patch(f"{_REC}.build_radarr_from_db", return_value=None):
        client.post(f"/api/recommended/{sid}/download")
    assert _audit(conn) == []
