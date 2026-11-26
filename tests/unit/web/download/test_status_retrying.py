"""/api/download/status of a download that needs attention says whether mediaman will remove
it and look for another copy unasked, so the page a download was asked from can say so."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.crypto import generate_poll_token
from mediaman.db import init_db, set_connection
from mediaman.web.routes.download import reset_download_caches
from mediaman.web.routes.download import router as download_router
from tests.helpers.factories import insert_settings

_WHY = "Single episode file contains all episodes in seasons. Review file name or manually import"


def _stuck(why: str = _WHY, season: int = 12) -> dict:
    return {
        "series": {"id": 7, "title": "Alone", "tmdbId": 63726},
        "seriesId": 7,
        "episode": {"seasonNumber": season, "episodeNumber": 1},
        "size": 1_000,
        "sizeleft": 0,
        "status": "completed",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importPending",
        "statusMessages": [{"title": "Alone.S12", "messages": [why]}],
        "downloadId": "nzb-1",
    }


@pytest.fixture
def conn(db_path):
    conn = init_db(str(db_path))
    set_connection(conn)
    return conn


@pytest.fixture
def poll(conn, secret_key):
    reset_download_caches()
    app = FastAPI()
    app.include_router(download_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    sonarr, radarr = MagicMock(), MagicMock()

    def ask(service: str, queue: list[dict]) -> dict:
        reset_download_caches()
        (sonarr if service == "sonarr" else radarr).get_queue.return_value = queue
        tmdb_id = 63726 if service == "sonarr" else 550
        token = generate_poll_token(
            media_item_id="", service=service, tmdb_id=tmdb_id, secret_key=secret_key
        )
        url = f"/api/download/status?service={service}&tmdb_id={tmdb_id}&poll_token={token}"
        return TestClient(app).get(url).json()

    radarr.get_movie_by_tmdb.return_value = {"id": 3, "tmdbId": 550, "hasFile": False}
    status = "mediaman.web.routes.download.status"
    with (
        patch(f"{status}.build_sonarr_from_db", return_value=sonarr),
        patch(f"{status}.build_radarr_from_db", return_value=radarr),
    ):
        yield ask
    reset_download_caches()


def _tried(conn, key: str, tries: int) -> None:
    conn.execute("INSERT INTO arr_import_retries (key, tries) VALUES (?, ?)", (key, tries))
    conn.commit()


def test_a_stuck_season_with_tries_left_will_get_another_copy(poll, conn):
    _tried(conn, "sonarr:7:12", 1)
    data = poll("sonarr", [_stuck()])
    assert (data["state"], data["retrying"]) == ("needs_attention", True)


def test_a_stuck_season_whose_tries_are_spent_is_left_for_a_person(poll, conn):
    _tried(conn, "sonarr:7:12", 99)
    assert poll("sonarr", [_stuck()])["retrying"] is False


def test_nothing_is_tried_again_while_the_setting_is_off(poll, conn):
    insert_settings(conn, import_retry_enabled="false")
    assert poll("sonarr", [_stuck()])["retrying"] is False


def test_a_fault_of_the_setup_is_not_tried_again(poll):
    stuck = _stuck("Import failed, path does not exist or is not accessible by Sonarr")
    assert poll("sonarr", [stuck])["retrying"] is False


def test_a_stuck_film_is_tried_again_only_while_it_has_tries_left(poll, conn):
    _tried(conn, "radarr:3", 99)
    film = {
        **_stuck(),
        "movie": {"id": 3, "title": "Fight Club", "tmdbId": 550},
        "movieId": 3,
    }
    assert poll("radarr", [film])["retrying"] is False
    conn.execute("UPDATE arr_import_retries SET tries = 0")
    conn.commit()
    assert poll("radarr", [film])["retrying"] is True
