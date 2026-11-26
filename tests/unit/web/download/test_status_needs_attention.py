"""/api/download/status of a show Sonarr will not import without a person: it needs attention,
and only an admin is told what Sonarr says of it."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.crypto import generate_poll_token
from mediaman.db import init_db, set_connection
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.download import reset_download_caches
from mediaman.web.routes.download import router as download_router

_WHY = "Single episode file contains all episodes in seasons. Review file name or manually import"
_STUCK = {
    "title": "Alone.S12.1080p.WEB-DL",
    "series": {"id": 7, "title": "Alone", "tmdbId": 63726},
    "episode": {"seasonNumber": 12, "episodeNumber": 1},
    "size": 1_000,
    "sizeleft": 0,
    "status": "completed",
    "trackedDownloadStatus": "warning",
    "trackedDownloadState": "importPending",
    "statusMessages": [{"title": "Alone.S12.1080p.WEB-DL", "messages": [_WHY]}],
    "downloadId": "nzb-1",
}


@pytest.fixture
def app(db_path, secret_key):
    reset_download_caches()
    conn = init_db(str(db_path))
    app = FastAPI()
    app.include_router(download_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    sonarr = MagicMock()
    sonarr.get_queue.return_value = [_STUCK]
    with patch("mediaman.web.routes.download.status.build_sonarr_from_db", return_value=sonarr):
        yield app
    reset_download_caches()


def test_an_admin_is_told_it_needs_attention_and_why(app, db_path):
    create_user(app.state.db, "admin", "password1234", enforce_policy=False)
    client = TestClient(app)
    client.cookies.set("session_token", create_session(app.state.db, "admin"))
    data = client.get("/api/download/status?service=sonarr&tmdb_id=63726").json()
    assert (data["state"], data["state_label"]) == ("needs_attention", "Needs attention")
    assert data["attention"] == _WHY
    assert data["attention_hint"] == "Fix it in Sonarr: Activity → Queue"
    assert data["eta"] == ""


def test_one_polling_with_a_token_is_told_only_that_it_needs_attention(app, secret_key):
    token = generate_poll_token(
        media_item_id="", service="sonarr", tmdb_id=63726, secret_key=secret_key
    )
    data = (
        TestClient(app)
        .get(f"/api/download/status?service=sonarr&tmdb_id=63726&poll_token={token}")
        .json()
    )
    assert data["state"] == "needs_attention"
    assert (data["attention"], data["attention_hint"], data["arr_source"]) == ("", "", "")


def test_the_downloads_page_tells_a_viewer_nothing_of_what_sonarr_says():
    from mediaman.web.routes.downloads import _for_viewer

    item = {"id": "sonarr:Alone", "title": "Alone", "state": "needs_attention"}
    page = {
        "hero": {**item, "attention": _WHY, "attention_hint": "Fix it in Sonarr: Activity → Queue"},
        "queue": [{**item, "attention": _WHY, "attention_hint": "Fix it"}],
        "upcoming": [],
        "recent": [],
    }
    _for_viewer(page)
    for shown in (page["hero"], *page["queue"]):
        assert (shown["state"], shown["attention"], shown["attention_hint"]) == (
            "needs_attention",
            "",
            "",
        )
