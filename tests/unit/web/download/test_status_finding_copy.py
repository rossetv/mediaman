"""/api/download/status of a title whose download Radarr or Sonarr would not import and mediaman
removed: another copy is looked for, which is not the same as searching."""

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


@pytest.fixture
def app(db_path, secret_key):
    reset_download_caches()
    conn = init_db(str(db_path))
    app = FastAPI()
    app.include_router(download_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    radarr, sonarr = MagicMock(), MagicMock()
    radarr.get_movie_by_tmdb.return_value = {"id": 4, "title": "Meridian", "hasFile": False}
    radarr.get_queue.return_value = []
    sonarr.get_queue.return_value = []
    sonarr.get_series.return_value = [
        {"id": 7, "title": "Alone", "tmdbId": 63726, "monitored": True, "seasons": []}
    ]
    base = "mediaman.web.routes.download.status"
    with (
        patch(f"{base}.build_radarr_from_db", return_value=radarr),
        patch(f"{base}.build_sonarr_from_db", return_value=sonarr),
    ):
        yield app
    reset_download_caches()


def _retry(app, key):
    app.state.db.execute(
        "INSERT INTO arr_import_retries (key, stuck_since, retried_at) VALUES (?, ?, ?)",
        (key, "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"),
    )
    app.state.db.commit()


def _poll(app, secret_key, service, tmdb_id, media_item_id=""):
    token = generate_poll_token(
        media_item_id=media_item_id, service=service, tmdb_id=tmdb_id, secret_key=secret_key
    )
    url = f"/api/download/status?service={service}&tmdb_id={tmdb_id}&poll_token={token}"
    return TestClient(app).get(url).json()


def test_a_film_whose_download_was_removed_to_find_another_copy_says_so(app, secret_key):
    assert _poll(app, secret_key, "radarr", 77)["state"] == "searching"
    reset_download_caches()
    _retry(app, "radarr:4")

    data = _poll(app, secret_key, "radarr", 77)

    assert (data["state"], data["state_label"]) == ("finding_copy", "Finding another copy")


def test_a_show_with_a_season_looked_for_again_says_so_and_another_shows_does_not(app, secret_key):
    _retry(app, "sonarr:8:1")
    assert _poll(app, secret_key, "sonarr", 63726)["state"] == "searching"
    reset_download_caches()
    _retry(app, "sonarr:7:1")

    assert _poll(app, secret_key, "sonarr", 63726)["state"] == "finding_copy"
