"""What the abandon route acts on is looked up live, never from a reused answer."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests
from fastapi import HTTPException

from mediaman.services.downloads.download_queue import (
    _reset_previous_queue,
    build_downloads_response,
)
from mediaman.web.routes.downloads import _lookup_dl_item

KEY = "k" * 64


@pytest.fixture
def radarr(monkeypatch):
    _reset_previous_queue()
    client, sonarr = MagicMock(), MagicMock()
    client.get_queue.return_value = [
        {
            "title": "Dune.2021.1080p",
            "size": 100,
            "sizeleft": 70,
            "status": "downloading",
            "movie": {"title": "Dune"},
        }
    ]
    sonarr.get_queue.return_value = []
    client.get_movies.return_value = []
    sonarr.get_series.return_value = []
    monkeypatch.setattr("mediaman.services.arr.build.build_radarr_from_db", lambda *a: client)
    monkeypatch.setattr("mediaman.services.arr.build.build_sonarr_from_db", lambda *a: sonarr)
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.build_nzbget_from_db", lambda *a: None
    )
    yield client
    _reset_previous_queue()


def test_a_lookup_that_leads_to_an_action_refuses_when_its_service_just_failed(conn, radarr):
    shown = build_downloads_response(conn, KEY)["hero"]["id"]
    radarr.get_queue.side_effect = requests.ConnectionError("down")

    with pytest.raises(HTTPException) as caught:
        _lookup_dl_item(conn, KEY, shown)

    assert caught.value.status_code == 503
