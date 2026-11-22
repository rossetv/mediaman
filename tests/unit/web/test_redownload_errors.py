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


def _post(client, payload):
    return client.post("/api/media/redownload", json=payload)


def test_every_failure_carries_its_human_reason_under_message(app_factory, authed_client, conn):
    client = _client(app_factory, authed_client, conn)

    with patch(RADARR, return_value=None):
        resp = _post(client, {"title": "Dune", "tmdb_id": 42})
    assert (resp.status_code, resp.json()["message"]) == (409, "Radarr is not configured")
    assert resp.json()["error"] == "Radarr is not configured"

    resp = _post(client, {"title": "  "})
    assert (resp.status_code, resp.json()["message"]) == (400, "No title provided")

    resp = _post(client, {"title": "Dune"})
    assert resp.status_code == 400
    assert resp.json()["message"] == resp.json()["error"]
    assert "tmdb_id" in resp.json()["message"]

    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    radarr.add_movie.side_effect = _err(409)
    with patch(RADARR, return_value=radarr):
        resp = _post(client, {"title": "Dune", "tmdb_id": 42})
    assert resp.json()["message"] == "'Dune' already exists in Radarr"

    radarr.add_movie.side_effect = _err(500, "boom")
    with patch(RADARR, return_value=radarr):
        resp = _post(client, {"title": "Dune", "tmdb_id": 42})
    assert resp.json()["message"].startswith("Download request failed")
    assert resp.json()["error"] == resp.json()["message"]


def test_throttled_redownload_carries_its_reason_under_message(app_factory, authed_client, conn):
    client = _client(app_factory, authed_client, conn)
    with patch(RADARR, return_value=None):
        for _ in range(21):
            resp = _post(client, {"title": "Dune", "tmdb_id": 42})
    assert resp.status_code == 429
    assert resp.json()["message"] == "Too many redownload requests — slow down"
