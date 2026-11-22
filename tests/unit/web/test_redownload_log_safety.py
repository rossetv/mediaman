"""A re-download title with line breaks cannot start a forged log line."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from mediaman.services.infra import SafeHTTPError
from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.library_api import router as library_api_router
from mediaman.web.routes.library_api.redownload import _REDOWNLOAD_LIMITER

RADARR = "mediaman.web.routes.library_api.build_radarr_from_db"
SONARR = "mediaman.web.routes.library_api.build_sonarr_from_db"
EVIL = "Dune\r\nFORGED line"


@pytest.fixture(autouse=True)
def _reset_limiter():
    _REDOWNLOAD_LIMITER.reset()


def _post(app_factory, authed_client, conn, payload, caplog):
    app = app_factory(library_router, library_api_router, conn=conn)
    client = authed_client(app, conn)
    with caplog.at_level(logging.INFO, logger="mediaman.web.routes.library_api"):
        client.post("/api/media/redownload", json=payload)


def _assert_no_raw_breaks(caplog):
    lines = [r.getMessage() for r in caplog.records if "FORGED" in r.getMessage()]
    assert lines
    assert not any("\r" in m or "\n" in m for m in lines)


def test_movie_success_log_escapes_title(app_factory, authed_client, conn, caplog):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    with patch(RADARR, return_value=radarr):
        _post(app_factory, authed_client, conn, {"title": EVIL, "tmdb_id": 42}, caplog)
    _assert_no_raw_breaks(caplog)


def test_season_success_log_escapes_title(app_factory, authed_client, conn, caplog):
    sonarr = MagicMock()
    sonarr.get_series_by_tvdb.return_value = None
    with patch(SONARR, return_value=sonarr):
        _post(
            app_factory,
            authed_client,
            conn,
            {"title": EVIL, "tvdb_id": 9, "season_number": 1},
            caplog,
        )
    _assert_no_raw_breaks(caplog)


def test_failure_log_escapes_title(app_factory, authed_client, conn, caplog):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    radarr.add_movie.side_effect = SafeHTTPError(
        status_code=500, body_snippet="boom", url="http://arr/x"
    )
    with patch(RADARR, return_value=radarr):
        _post(app_factory, authed_client, conn, {"title": EVIL, "tmdb_id": 42}, caplog)
    _assert_no_raw_breaks(caplog)


def test_failure_log_escapes_client_exception_text(app_factory, authed_client, conn, caplog):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    radarr.add_movie.side_effect = SafeHTTPError(
        status_code=500, body_snippet="boom\r\nFORGED line", url="http://arr/x"
    )
    with patch(RADARR, return_value=radarr):
        _post(app_factory, authed_client, conn, {"title": "Dune", "tmdb_id": 42}, caplog)
    _assert_no_raw_breaks(caplog)
    fmt = logging.Formatter("%(message)s")
    rendered = "\n".join(
        fmt.format(r) for r in caplog.records if r.getMessage().startswith("Re-download via")
    )
    assert "FORGED" in rendered
    assert "\nFORGED" not in rendered
    assert "\r" not in rendered
