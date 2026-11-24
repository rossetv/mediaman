"""What /downloads says when Radarr, Sonarr or NZBGet cannot be reached."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from mediaman.services.downloads.download_queue import _reset_previous_queue
from mediaman.web.routes.downloads import router as downloads_router

_DOWN = requests.ConnectionError("http://10.0.0.9:7878 refused")


@pytest.fixture
def services(monkeypatch):
    """Radarr, Sonarr and NZBGet, all up and idle."""
    _reset_previous_queue()
    radarr, sonarr, nzbget = MagicMock(), MagicMock(), MagicMock()
    for client in (radarr, sonarr, nzbget):
        client.get_queue.return_value = []
    radarr.get_movies.return_value = []
    sonarr.get_series.return_value = []
    nzbget.get_status.return_value = {}
    for target, client in (
        ("arr.build.build_radarr_from_db", radarr),
        ("arr.build.build_sonarr_from_db", sonarr),
        ("downloads.download_queue.build_nzbget_from_db", nzbget),
    ):
        monkeypatch.setattr(f"mediaman.services.{target}", lambda *a, client=client: client)
    yield radarr, sonarr, nzbget
    _reset_previous_queue()


@pytest.fixture
def client(app_factory, authed_client, conn, templates_stub):
    app = app_factory(downloads_router, conn=conn, state_extras={"templates": templates_stub})
    return authed_client(app, conn)


def _get(client) -> dict:
    return client.get("/api/downloads").json()


def test_nothing_is_unreachable_while_all_answer(services, client):
    data = _get(client)
    assert (data["unreachable"], data["outage"], data["subtitle"]) == ([], "", "")


def test_every_service_down_is_named_without_internals(services, client):
    radarr, sonarr, nzbget = services
    radarr.get_queue.side_effect = sonarr.get_queue.side_effect = _DOWN
    nzbget.get_status.side_effect = _DOWN
    resp = client.get("/api/downloads")
    assert resp.json()["unreachable"] == ["Radarr", "Sonarr", "NZBGet"]
    assert "Radarr, Sonarr and NZBGet" in resp.json()["outage"]
    assert "10.0.0.9" not in resp.text


def test_a_list_radarr_gives_only_in_part_counts_as_unreachable(services, client):
    services[0].get_movies.side_effect = _DOWN
    assert _get(client)["unreachable"] == ["Radarr"]


def test_the_subtitle_names_the_outage_beside_what_is_in_progress(services, client):
    radarr, _, nzbget = services
    radarr.get_queue.side_effect = _DOWN
    nzbget.get_queue.return_value = [
        {"NZBName": "Dune.2021", "Status": "DOWNLOADING", "FileSizeMB": 10, "RemainingSizeMB": 5}
    ]
    subtitle = _get(client)["subtitle"]
    assert "1 item in progress" in subtitle
    assert "Radarr" in subtitle


def test_abandon_during_an_outage_says_so_instead_of_not_found(services, client):
    services[0].get_queue.side_effect = _DOWN
    resp = client.post("/api/downloads/radarr%3ADune/abandon", json={})
    assert resp.status_code == 503
    assert "Radarr" in resp.json()["detail"]
