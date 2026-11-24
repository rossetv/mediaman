"""What /downloads says when Radarr, Sonarr or NZBGet cannot be reached."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.spec import RADARR_SPEC, SONARR_SPEC
from mediaman.services.downloads.download_queue import _reset_previous_queue
from mediaman.services.downloads.nzbget import NzbgetClient
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


@pytest.mark.parametrize("service", ["Radarr", "Sonarr", "NZBGet"])
@pytest.mark.parametrize("answer", [{"error": "x"}, ["x"], {"records": ["x"], "totalRecords": 1}])
def test_a_service_that_answers_with_the_wrong_shape_is_unreachable(
    client, monkeypatch, services, fake_http, fake_response, service, answer
):
    broken = {
        "Radarr": ("arr.build.build_radarr_from_db", ArrClient(RADARR_SPEC, "http://r", "k")),
        "Sonarr": ("arr.build.build_sonarr_from_db", ArrClient(SONARR_SPEC, "http://s", "k")),
        "NZBGet": (
            "downloads.download_queue.build_nzbget_from_db",
            NzbgetClient("http://n", "u", "p"),
        ),
    }
    target, real = broken[service]
    monkeypatch.setattr(f"mediaman.services.{target}", lambda *a: real)
    if service == "NZBGet":
        fake_http.queue("POST", fake_response(json_data={"result": {}}))
        answer = {"result": answer}
    fake_http.default(fake_response(json_data=answer))

    resp = client.get("/api/downloads")

    assert resp.status_code == 200
    assert resp.json()["unreachable"] == [service]


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


_POSTER = "https://artworks.thetvdb.com/banners/posters/dune.jpg"


def _queued(title: str, release: str) -> dict[str, object]:
    images = [{"coverType": "poster", "remoteUrl": _POSTER}]
    return {
        "title": release,
        "size": 100,
        "sizeleft": 40,
        "status": "downloading",
        "movie": {"title": title, "images": images},
        "series": {"id": 7, "title": title, "images": images},
        "episode": {"seasonNumber": 1, "episodeNumber": 1, "title": "One"},
    }


def _loading(name: str) -> dict[str, object]:
    return {"NZBName": name, "Status": "DOWNLOADING", "FileSizeMB": 10, "RemainingSizeMB": 5}


_SERVICES = pytest.mark.parametrize(("service", "name"), [(0, "radarr"), (1, "sonarr")])


@_SERVICES
def test_what_finishes_while_its_service_is_down_is_recorded_once_it_is_back(
    services, client, service, name
):
    arr, other, nzbget = services[service], services[1 - service], services[2]
    arr.get_queue.return_value = [_queued("Dune", "Dune.2021.1080p")]
    nzbget.get_queue.return_value = [_loading("Dune.2021.1080p")]
    _get(client)

    arr.get_queue.side_effect = _DOWN
    nzbget.get_queue.return_value = []
    other.get_queue.return_value = [_queued("Other", "Other")]
    assert _get(client)["recent"] == []
    _get(client)

    arr.get_queue.side_effect = None
    arr.get_queue.return_value = []
    arr.get_movies.return_value = [{"id": 1, "title": "Dune", "hasFile": True}]
    arr.get_series.return_value = [
        {"id": 7, "title": "Dune", "statistics": {"episodeFileCount": 1}}
    ]
    arr.get_episodes.return_value = [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True}]
    assert [r["id"] for r in _get(client)["recent"]] == [f"{name}:Dune"]


def test_an_item_its_service_left_out_of_half_an_answer_is_not_taken_for_finished(services, client):
    radarr = services[0]
    radarr.get_movies.return_value = [
        {"id": 1, "title": "Dune", "monitored": True, "isAvailable": True}
    ]
    _get(client)

    radarr.get_movies.side_effect = _DOWN
    _get(client)

    radarr.get_movies.side_effect = None
    radarr.get_movies.return_value = [
        {"id": 1, "title": "Dune", "monitored": True, "hasFile": True}
    ]
    assert [r["id"] for r in _get(client)["recent"]] == ["radarr:Dune"]


@_SERVICES
def test_a_page_first_loaded_while_a_service_is_down_keeps_the_names_and_art_of_its_downloads(
    services, client, service, name
):
    arr, nzbget = services[service], services[2]
    arr.get_queue.return_value = [_queued("Dune: Part One", "dune.p1.2021.1080p")]
    nzbget.get_queue.return_value = [_loading("dune.p1.2021.1080p"), _loading("Manual.2020")]
    before = _get(client)

    arr.get_queue.side_effect = _DOWN
    for _ in range(2):
        data = _get(client)
        items = [data["hero"], *data["queue"]]
        assert [(i["id"], i["title"], i["poster_url"]) for i in items] == [
            (f"{name}:Dune: Part One", "Dune: Part One", _POSTER),
            ("Manual.2020", "Manual", ""),
        ]
        assert data["recent"] == []
    assert before["hero"]["title"] == "Dune: Part One"

    # what NZBGet no longer holds is not made up from memory
    nzbget.get_queue.return_value = [_loading("Manual.2020")]
    data = _get(client)
    assert (data["hero"]["title"], data["queue"]) == ("Manual", [])


def _show(title: str, *files: int) -> dict[str, object]:
    """A show of eight-episode seasons, all aired and monitored, with *files* of each."""
    seasons = [
        {
            "seasonNumber": n,
            "monitored": True,
            "statistics": {
                "episodeFileCount": have,
                "episodeCount": 8,
                "previousAiring": "2020-01-01T00:00:00Z",
            },
        }
        for n, have in enumerate(files, start=1)
    ]
    stats = {"episodeFileCount": sum(files)}
    return {"id": len(title), "title": title, "seasons": seasons, "statistics": stats}


def test_a_recent_show_lacking_an_aired_season_is_there_in_part_until_it_is_whole(
    services, client, conn, monkeypatch
):
    sonarr = services[1]
    clock = [1000.0]
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.time.monotonic", lambda: clock[0]
    )
    with conn:
        conn.executemany(
            "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES (?, ?, ?)",
            [
                ("sonarr:Half", "Half", "series"),
                ("sonarr:Whole", "Whole", "series"),
                ("radarr:Half", "Half", "movie"),
            ],
        )
    sonarr.get_series.return_value = [_show("Half", 8, 0), _show("Whole", 8, 8)]

    def partial() -> dict[str, bool]:
        return {r["id"]: r["partial"] for r in _get(client)["recent"]}

    in_part = {"sonarr:Half": True, "sonarr:Whole": False, "radarr:Half": False}
    assert partial() == in_part

    clock[0] += 3600
    sonarr.get_queue.side_effect = sonarr.get_series.side_effect = _DOWN
    assert partial() == in_part, "an outage tells nothing new"

    sonarr.get_queue.side_effect = sonarr.get_series.side_effect = None
    sonarr.get_series.return_value = [_show("Half", 8, 8), _show("Whole", 8, 8)]
    assert set(partial().values()) == {False}
