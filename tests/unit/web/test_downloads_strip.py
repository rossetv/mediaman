"""GET /api/downloads/strip: one answer for everybody, with titles a viewer may open."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.core.time import now_utc
from mediaman.services.downloads import strip
from mediaman.services.downloads.download_queue import _reset_previous_queue
from mediaman.services.watch_catalog import clear_library_index, invalidate
from mediaman.web.auth.session_store import create_session
from mediaman.web.middleware.viewer_guard import ViewerGuardMiddleware
from mediaman.web.routes.downloads import router as downloads_router
from mediaman.web.routes.downloads_strip import router
from tests.helpers.factories import insert_media_item

_TMDB = "https://image.tmdb.org/t/p/original"


def _movie(n: int, title: str, **over: object) -> dict[str, object]:
    return {
        "id": n,
        "tmdbId": n,
        "title": title,
        "year": 2024,
        "monitored": True,
        "hasFile": False,
        "isAvailable": True,
        "images": [
            {"coverType": "poster", "remoteUrl": f"{_TMDB}/p{n}.jpg"},
            {"coverType": "fanart", "remoteUrl": f"{_TMDB}/f{n}.jpg"},
        ],
    } | over


@pytest.fixture
def services(monkeypatch):
    """Radarr, Sonarr and NZBGet; Radarr downloads two films."""

    def clear():
        _reset_previous_queue()
        strip.shared.clear()
        clear_library_index()
        invalidate()

    clear()
    radarr, sonarr, nzbget = MagicMock(), MagicMock(), MagicMock()
    for client in (radarr, sonarr, nzbget):
        client.get_queue.return_value = []
    _queue((radarr,), [_movie(11, "Ember Coast"), _movie(12, "Dry Harbour")])
    sonarr.get_series.return_value = []
    nzbget.get_status.return_value = {}
    for target, client in (
        ("arr.build.build_radarr_from_db", radarr),
        ("arr.build.build_sonarr_from_db", sonarr),
        ("downloads.download_queue.build_nzbget_from_db", nzbget),
    ):
        monkeypatch.setattr(f"mediaman.services.{target}", lambda *a, client=client: client)
    yield radarr, sonarr, nzbget
    clear()


@pytest.fixture
def viewer(app_factory, conn):
    from fastapi.testclient import TestClient

    # Not create_user: hashing a password takes a second.
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, role, created_at)"
        " VALUES ('guest', 'x', 'viewer', '2026-01-01')"
    )
    app = app_factory(router, downloads_router, conn=conn)
    app.add_middleware(ViewerGuardMiddleware)
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "guest"))
    return client


@pytest.fixture
def asked(monkeypatch):
    """What TMDB told before, by its id, and whom it is asked of; an exception is raised."""
    held: dict[int, object] = {}
    ids: list[int] = []

    def held_details(_kind, tmdb_id):
        ids.append(tmdb_id)
        if isinstance(held.get(tmdb_id), Exception):
            raise held[tmdb_id]
        return held.get(tmdb_id)

    monkeypatch.setattr("mediaman.services.watch_catalog.held_details", held_details)
    return held, ids


def _queue(services, movies):
    radarr = services[0]
    radarr.get_movies.return_value = movies
    radarr.get_queue.return_value = [
        {
            "movie": movie,
            "title": movie["title"],
            "status": "downloading",
            "size": 1000,
            "sizeleft": 600,
            "downloadId": str(movie["id"]),
        }
        for movie in movies
    ]


def _get(client):
    return client.get("/api/downloads/strip")


def test_a_viewer_gets_every_download_with_a_title_to_open(services, viewer, conn):
    insert_media_item(conn, id="900", title="Dry Harbour", media_type="movie")
    conn.execute("UPDATE media_items SET tmdb_id = 12")
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('plex_libraries', '[1]', '2026-01-01')"
    )
    conn.commit()
    body = _get(viewer).json()
    ember, harbour = body["items"]
    assert (ember["state"], ember["percent"]) == ("downloading", 40)
    assert (ember["ref"], ember["href"]) == ("tmdb-movie-11", "/watch#title=tmdb-movie-11")
    assert ember["poster_url"] == "/api/watch/img/tmdb/w342/p11.jpg"
    assert ember["art_url"] == "/api/watch/img/tmdb/w1280/f11.jpg"
    assert (harbour["ref"], harbour["href"]) == ("movie-900", "/watch/movie/900")
    assert body["summary"] == {"total": 2, "active": 2, "waiting": 0, "ready": 0, "percent": 40}


def test_a_viewer_is_told_nothing_the_downloads_page_keeps_from_one(services, viewer):
    body = _get(viewer)
    assert not {"arr_link", "arr_id", "arr_source", "abandon_visible", "queued_since"} & set(
        body.json()["items"][0]
    )
    assert "image.tmdb.org" not in body.text


def test_a_service_that_stops_answering_leaves_the_row_as_it_was(services, viewer, monkeypatch):
    monkeypatch.setattr(strip, "HOLD_S", 0.0)
    _get(viewer)
    services[0].get_queue.side_effect = requests.ConnectionError("http://10.0.0.9:7878 refused")
    services[0].get_movies.side_effect = services[0].get_queue.side_effect
    resp = _get(viewer)
    body = resp.json()
    assert [(i["title"], i["percent"], i["held"]) for i in body["items"]] == [
        ("Ember Coast", 40, True),
        ("Dry Harbour", 40, True),
    ]
    assert body["outage"] == "Can't reach Radarr"
    assert "10.0.0.9" not in resp.text


def test_when_nothing_is_known_and_it_fails_it_says_so_rather_than_nothing_downloads(
    services, viewer, monkeypatch
):
    def broken(*_args):
        raise RuntimeError("no")

    monkeypatch.setattr("mediaman.web.routes.downloads_strip.build_downloads_response", broken)
    resp = _get(viewer)
    assert (resp.status_code, resp.json()["error"]) == (503, "busy")


def test_two_people_asking_cost_the_services_one_ask(services, viewer):
    _get(viewer)
    asked = services[0].get_queue.call_count
    _get(viewer)
    assert services[0].get_queue.call_count == asked


def test_nobody_is_told_without_a_session(services, viewer):
    viewer.cookies.clear()
    assert _get(viewer).status_code == 401


def test_asking_is_no_sign_of_anyone_there(services, viewer, conn):
    """The header asks from every open page, which must not keep it signed in for good."""
    long_ago = (now_utc() - timedelta(hours=2)).isoformat()
    conn.execute("UPDATE admin_sessions SET last_used_at = ?", (long_ago,))
    conn.commit()

    def used() -> str:
        return conn.execute("SELECT last_used_at FROM admin_sessions").fetchone()[0]

    assert _get(viewer).status_code == 200
    assert used() == long_ago
    viewer.get("/api/downloads")
    assert used() != long_ago


def test_a_session_left_alone_too_long_ends_for_this_too(services, viewer, conn):
    conn.execute(
        "UPDATE admin_sessions SET last_used_at = ?",
        ((now_utc() - timedelta(hours=25)).isoformat(),),
    )
    conn.commit()
    assert _get(viewer).status_code == 401


def test_a_title_radarr_has_no_picture_of_gets_those_tmdb_told_of(services, viewer, asked):
    held, ids = asked
    _queue(services, [_movie(11, "Ember Coast", images=[]), _movie(12, "Dry Harbour")])
    held[11] = {"poster_url": "/api/watch/img/tmdb/w342/a.jpg", "backdrop_url": "/art/b.jpg"}
    ember, harbour = _get(viewer).json()["items"]
    assert (ember["poster_url"], ember["art_url"]) == (
        "/api/watch/img/tmdb/w342/a.jpg",
        "/art/b.jpg",
    )
    assert harbour["poster_url"] == "/api/watch/img/tmdb/w342/p12.jpg"
    assert ids == [11]


def test_only_what_is_shown_is_looked_up(services, viewer, asked):
    over = strip.MAX_ITEMS + 5
    _queue(services, [_movie(n, f"Film No{n}", images=[]) for n in range(100, 100 + over)])
    body = _get(viewer).json()
    assert body["summary"]["total"] == over
    assert len(asked[1]) == strip.MAX_ITEMS


def test_a_title_whose_lookup_fails_takes_no_other_down(services, viewer, asked):
    _queue(services, [_movie(11, "Ember Coast", images=[]), _movie(12, "Dry Harbour")])
    asked[0][11] = AttributeError("malformed")
    resp = _get(viewer)
    assert resp.status_code == 200
    ember, harbour = resp.json()["items"]
    assert (ember["ref"], ember["poster_url"]) == ("tmdb-movie-11", "")
    assert harbour["art_url"] == "/api/watch/img/tmdb/w1280/f12.jpg"
