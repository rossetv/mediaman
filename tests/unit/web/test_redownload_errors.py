"""A re-download failure is only reported as a duplicate when the add call was refused."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mediaman.services.infra import SafeHTTPError
from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.library_api import router as library_api_router
from mediaman.web.routes.library_api.redownload import _REDOWNLOAD_LIMITER

RADARR = "mediaman.web.routes.library_api.build_radarr_from_db"
SONARR = "mediaman.web.routes.library_api.build_sonarr_from_db"


@pytest.fixture(autouse=True)
def _reset_limiter():
    _REDOWNLOAD_LIMITER.reset()


def _err(status: int, body: str = "conflict") -> SafeHTTPError:
    return SafeHTTPError(status_code=status, body_snippet=body, url="http://arr/api/v3/x")


def _client(app_factory, authed_client, conn):
    app = app_factory(library_router, library_api_router, conn=conn)
    return authed_client(app, conn)


@pytest.mark.parametrize("status", [409, 422])
def test_failed_movie_remonitor_is_a_failure_not_a_duplicate(
    app_factory, authed_client, conn, status
):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = {"id": 7}
    radarr.remonitor_movie.side_effect = _err(status)
    client = _client(app_factory, authed_client, conn)
    with patch(RADARR, return_value=radarr):
        resp = client.post("/api/media/redownload", json={"title": "Dune", "tmdb_id": 42})
    assert resp.json()["ok"] is False
    assert "already exists" not in resp.json()["error"]


def test_failed_season_remonitor_is_a_failure_not_a_duplicate(app_factory, authed_client, conn):
    sonarr = MagicMock()
    sonarr.get_series_by_tvdb.return_value = {"id": 3}
    sonarr.remonitor_season.side_effect = _err(409)
    client = _client(app_factory, authed_client, conn)
    with patch(SONARR, return_value=sonarr):
        resp = client.post(
            "/api/media/redownload",
            json={"title": "Severance", "tvdb_id": 999, "season_number": 1},
        )
    assert resp.json()["ok"] is False
    assert "already exists" not in resp.json()["error"]
