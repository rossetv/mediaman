"""A failed add must not block an immediate retry, and Arr errors must not become 500s."""

# ruff: noqa: F811

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.responses import JSONResponse

from mediaman.services.arr import ArrError
from mediaman.web.routes.search import download as dl
from tests.unit.web.test_search_download_rate_limit import (  # noqa: F401
    _setup_limiters,
    _valid_body,
    app,
    authed_client,
)

_MODULE = "mediaman.web.routes.search.download"


@pytest.fixture(autouse=True)
def _reset():
    _setup_limiters()


def test_failed_add_does_not_block_immediate_retry(authed_client):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    radarr.add_movie.side_effect = [ArrError("boom"), None]
    with patch(f"{_MODULE}.build_radarr_from_db", return_value=radarr):
        first = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=7))
        second = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=7))
    assert first.status_code == 502
    assert second.status_code == 200


def test_successful_add_still_suppresses_duplicate(authed_client):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    with patch(f"{_MODULE}.build_radarr_from_db", return_value=radarr):
        first = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=8))
        second = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=8))
    assert first.status_code == 200
    assert second.status_code == 429


def test_raising_submit_releases_the_dedup_key(authed_client):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    with (
        patch(f"{_MODULE}.build_radarr_from_db", return_value=radarr),
        patch(
            f"{_MODULE}._submit_movie",
            side_effect=[RuntimeError("boom"), JSONResponse({"ok": True})],
        ),
    ):
        with pytest.raises(RuntimeError):
            authed_client.post("/api/search/download", json=_valid_body(tmdb_id=9))
        retry = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=9))
    assert retry.status_code == 200


def test_arr_error_in_tvdb_lookup_is_a_plain_502(conn):
    sonarr = MagicMock()
    sonarr.lookup_series_by_tmdb.side_effect = ArrError("secret internal detail")
    body = MagicMock(tmdb_id=5, title="Show")
    with patch(f"{_MODULE}.build_sonarr_from_db", return_value=sonarr):
        resp = dl._submit_tv(conn, "k", body, None, "admin")
    assert resp.status_code == 502
    assert b"secret internal detail" not in resp.body


def test_arr_error_in_tracked_check_is_a_plain_503(conn):
    sonarr = MagicMock()
    sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 10}
    body = MagicMock(tmdb_id=5, title="Show")
    with (
        patch(f"{_MODULE}.build_sonarr_from_db", return_value=sonarr),
        patch(f"{_MODULE}.is_series_already_tracked", side_effect=ArrError("secret")),
    ):
        resp = dl._submit_tv(conn, "k", body, None, "admin")
    assert resp.status_code == 503
    assert b"secret" not in resp.body
