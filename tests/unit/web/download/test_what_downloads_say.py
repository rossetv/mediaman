"""What Downloads, the downloading strip and the mailed page say of the same downloads."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.services.downloads import strip
from mediaman.services.downloads.abandon import AbandonResult
from mediaman.services.downloads.download_queue import _reset_previous_queue
from mediaman.services.infra import SafeHTTPError
from mediaman.services.watch_catalog import clear_library_index, invalidate
from mediaman.web.routes.download.status._radarr import _radarr_queue_item
from mediaman.web.routes.downloads import router as downloads_router
from mediaman.web.routes.downloads_strip import _with_titles
from mediaman.web.routes.downloads_strip import router as strip_router
from tests.helpers.factories import insert_media_item


def _movie(n: int, title: str) -> dict[str, object]:
    return {"id": n, "tmdbId": n, "title": title, "year": 2024, "monitored": True, "images": []}


def _clear() -> None:
    _reset_previous_queue()
    strip.shared.clear()
    strip.on_page.clear()
    clear_library_index()
    invalidate()


@pytest.fixture
def services(monkeypatch):
    """Radarr, Sonarr and NZBGet, all up and idle; *built* says which are set up."""
    _clear()
    radarr, sonarr, nzbget = MagicMock(), MagicMock(), MagicMock()
    for client in (radarr, sonarr, nzbget):
        client.get_queue.return_value = []
    radarr.get_movies.return_value = []
    sonarr.get_series.return_value = []
    nzbget.get_status.return_value = {}
    built = {"radarr": radarr, "sonarr": sonarr}
    for name in ("radarr", "sonarr"):
        monkeypatch.setattr(
            f"mediaman.services.arr.build.build_{name}_from_db",
            lambda *a, name=name: built[name],
        )
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.build_nzbget_from_db", lambda *a: nzbget
    )
    yield radarr, sonarr, nzbget, built
    _clear()


@pytest.fixture
def client(app_factory, authed_client, conn, templates_stub):
    app = app_factory(
        downloads_router, strip_router, conn=conn, state_extras={"templates": templates_stub}
    )
    return authed_client(app, conn)


def _as_viewer(conn) -> None:
    conn.execute("UPDATE admin_users SET role = 'viewer'")
    conn.commit()


def _queued(status: str) -> dict[str, object]:
    """Radarr's entry of a film its download client holds but has not begun."""
    return {
        "movie": _movie(11, "Ember Coast"),
        "title": "Ember.Coast.2024.1080p",
        "status": status,
        "trackedDownloadState": "downloading",
        "size": 1000,
        "sizeleft": 1000,
        "timeleft": "00:05:00",
        "downloadId": "11",
    }


@pytest.mark.parametrize("status", ["queued", "paused"])
def test_a_download_waiting_its_turn_reads_queued_with_no_time_left_everywhere(
    services, client, status
):
    radarr, _, nzbget, _ = services
    radarr.get_queue.return_value = [_queued(status)]
    nzbget.get_status.return_value = {"DownloadRate": 1_000_000}
    nzbget.get_queue.return_value = [
        {
            "NZBName": "Ember.Coast.2024.1080p",
            "Status": status.upper(),
            "FileSizeMB": 100,
            "RemainingSizeMB": 100,
        }
    ]

    hero = client.get("/api/downloads").json()["hero"]
    (item,) = client.get("/api/downloads/strip").json()["items"]
    mailed = _radarr_queue_item([_queued(status)], 11)

    assert (hero["state"], hero["state_label"], hero["eta"]) == ("queued", "Queued", "")
    assert (item["state"], item["minutes_left"]) == ("queued", None)
    assert mailed is not None
    assert (mailed["state"], mailed["eta"]) == ("queued", "")


def test_a_refused_key_is_named_to_an_admin_and_not_taken_for_the_network(services, client):
    services[0].get_queue.side_effect = SafeHTTPError(401, "", "http://radarr:7878/api/v3/queue")
    services[2].get_status.side_effect = SafeHTTPError(403, "", "http://nzbget:6789/jsonrpc")
    services[1].get_queue.side_effect = requests.ConnectionError("down")

    data = client.get("/api/downloads").json()
    ring = client.get("/api/downloads/strip").json()

    assert data["refused"] == ["Radarr", "NZBGet"]
    assert data["outage"] == "Can't reach Sonarr or sign in to Radarr and NZBGet"
    assert ring["outage"] == data["outage"]


def test_a_viewer_is_told_only_that_downloads_are_not_available_while_a_key_is_refused(
    services, client, conn
):
    _as_viewer(conn)
    services[1].get_queue.side_effect = SafeHTTPError(403, "", "http://sonarr:8989/api/v3/queue")

    data = client.get("/api/downloads").json()
    ring = client.get("/api/downloads/strip").json()

    assert data["outage"] == ring["outage"] == "Downloads aren't available"
    assert data["strip"]["outage"] == "Downloads aren't available"
    assert "Sonarr" not in data["subtitle"]


def test_the_page_says_which_of_radarr_and_sonarr_are_set_up(services, client):
    services[3]["radarr"] = None

    page = client.get("/downloads").json()
    data = client.get("/api/downloads").json()

    assert page["downloads_set_up"] == data["downloads_set_up"] == {"movies": False, "shows": True}


def test_a_failed_stop_tracking_answers_its_words_as_the_message(services, client):
    services[0].get_queue.side_effect = requests.ConnectionError("down")

    resp = client.post("/api/downloads/radarr%3ADune/abandon", json={})

    assert resp.status_code == 503
    assert resp.json()["message"] == resp.json()["detail"]
    assert "Radarr" in resp.json()["message"]


def test_stop_tracking_that_radarr_refuses_says_so_and_rebuilds_no_queue(services, client):
    item = {"id": "radarr:Dune", "arr_id": 4, "kind": "movie", "state": "searching"}
    with (
        patch("mediaman.web.routes.downloads._lookup_dl_item", return_value=item),
        patch(
            "mediaman.web.routes.downloads.abandon_movie",
            return_value=AbandonResult(kind="movie", failed=[0], dl_id="radarr:Dune"),
        ),
    ):
        resp = client.post("/api/downloads/radarr%3ADune/abandon", json={"arr_id": 4})

    assert resp.status_code == 502
    assert "Radarr" in resp.json()["message"]
    assert "queue" not in resp.json()
    services[0].get_queue.assert_not_called()


def _hold_in_library(conn, tmdb_id: int, title: str) -> None:
    insert_media_item(conn, id=str(900 + tmdb_id), title=title, media_type="movie")
    conn.execute("UPDATE media_items SET tmdb_id = ? WHERE title = ?", (tmdb_id, title))
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('plex_libraries', '[1]', '2026-01-01')"
    )
    conn.commit()


def test_a_recent_film_is_ready_to_watch_only_once_the_library_holds_it(services, client, conn):
    _hold_in_library(conn, 12, "Dry Harbour")
    conn.execute(
        "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES"
        " ('radarr:Ember Coast#11', 'Ember Coast', 'movie'),"
        " ('radarr:Dry Harbour#12', 'Dry Harbour', 'movie')"
    )
    conn.commit()

    recent = client.get("/api/downloads").json()["recent"]

    assert {r["title"]: r["readiness"] for r in recent} == {
        "Ember Coast": "unknown",
        "Dry Harbour": "ready",
    }


def test_the_strip_says_added_of_what_the_library_does_not_hold_yet(services, conn):
    _hold_in_library(conn, 12, "Dry Harbour")

    def ready(title: str, tmdb_id: int) -> strip.StripItem:
        return strip.StripItem(
            key=title, title=title, kind="movie", state="ready", tmdb_id=tmdb_id, ready_seconds=50
        )

    items = [ready("Ember Coast", 11), ready("Dry Harbour", 12)]
    told = _with_titles(conn, "k" * 64, items)

    assert [item.line for item in told] == ["Added", ""]


def test_the_strip_passes_on_no_other_service_s_poster(services, conn):
    show = strip.StripItem(
        key="k",
        title="Harbour",
        kind="show",
        state="downloading",
        poster_url="https://artworks.thetvdb.com/banners/posters/1.jpg",
    )

    (told,) = _with_titles(conn, "k" * 64, [show])

    assert told.poster_url == ""


def test_the_page_counts_in_progress_what_the_header_does_while_a_service_is_down(services, client):
    radarr, _, _, _ = services
    radarr.get_movies.return_value = [
        {**_movie(n, title), "hasFile": False, "isAvailable": True}
        for n, title in ((11, "Ember Coast"), (12, "Dry Harbour"))
    ]
    client.get("/api/downloads")
    radarr.get_queue.side_effect = radarr.get_movies.side_effect = requests.ConnectionError("x")

    data = client.get("/api/downloads").json()

    assert data["strip"]["summary"]["total"] == 2
    assert data["subtitle"] == "2 items in progress · can't reach Radarr"
