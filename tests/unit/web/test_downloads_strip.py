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
        strip.on_page.clear()
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
    harbour, ember = body["items"]
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


def test_the_downloads_page_gives_a_viewer_no_release_name(services, viewer, conn):
    _queue(services, [])
    named, obfuscated = "Some.Film.2024.1080p.WEB-GRP.nzb", "aB3xK9qLm2Zt"
    services[2].get_queue.return_value = [
        {"NZBName": name, "Status": "DOWNLOADING", "FileSizeMB": 100, "RemainingSizeMB": 40}
        for name in (named, obfuscated)
    ]
    conn.execute(
        "INSERT INTO recent_downloads (dl_id, title) VALUES ('Old.Film.2020.x264-GRP', 'Old Film'),"
        " ('zZ9yX8wV', 'zZ9yX8wV'), ('radarr:Ember Coast', 'Ember Coast')"
    )
    conn.commit()
    answer = viewer.get("/api/downloads")
    for name in (named, obfuscated, "Old.Film", "zZ9yX8wV"):
        assert name not in answer.text
    body = answer.json()
    active = [body["hero"], *body["queue"]]
    assert {item["title"] for item in active} == {"Some Film", strip.UNNAMED}
    ids = [item["id"] for item in active]
    assert len(set(ids)) == 2
    again = viewer.get("/api/downloads").json()
    assert [again["hero"]["id"], *(item["id"] for item in again["queue"])] == ids
    assert {item["title"] for item in body["recent"]} == {
        "Old Film",
        strip.UNNAMED,
        "Ember Coast",
    }
    assert "radarr:Ember Coast" in {item["id"] for item in body["recent"]}


def test_the_downloads_page_gives_an_admin_the_release_names(services, viewer, conn):
    _queue(services, [])
    services[2].get_queue.return_value = [{"NZBName": "aB3xK9qLm2Zt", "Status": "DOWNLOADING"}]
    conn.execute("UPDATE admin_users SET role = 'admin'")
    conn.commit()
    hero = viewer.get("/api/downloads").json()["hero"]
    assert (hero["id"], hero["title"]) == ("aB3xK9qLm2Zt", "aB3xK9qLm2Zt")


def test_a_service_that_stops_answering_leaves_in_the_row_only_what_downloads_shows(
    services, viewer, monkeypatch
):
    monkeypatch.setattr(strip, "HOLD_S", 0.0)
    _get(viewer)
    services[0].get_queue.side_effect = requests.ConnectionError("http://10.0.0.9:7878 refused")
    services[0].get_movies.side_effect = services[0].get_queue.side_effect
    resp = _get(viewer)
    body = resp.json()
    assert body["items"] == viewer.get("/api/downloads").json()["queue"] == []
    assert body["outage"] == "Downloads aren't available"
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
    harbour, ember = _get(viewer).json()["items"]
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
    harbour, ember = resp.json()["items"]
    assert (ember["ref"], ember["poster_url"]) == ("tmdb-movie-11", "")
    assert harbour["art_url"] == "/api/watch/img/tmdb/w1280/f12.jpg"


def test_what_is_ready_says_what_each_person_marked_it_as(viewer, conn, monkeypatch):
    insert_media_item(conn, id="900", title="Dry Harbour", media_type="movie")
    conn.execute("UPDATE media_items SET tmdb_id = 12, plex_rating_key = '900'")
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('plex_libraries', '[1]', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, role, created_at)"
        " VALUES ('other', 'x', 'viewer', '2026-01-01')"
    )
    guest, other = (
        conn.execute("SELECT id FROM admin_users WHERE username = ?", (name,)).fetchone()[0]
        for name in ("guest", "other")
    )
    for user, level, tmdb_id in ((guest, 2, 11), (other, -1, 12)):
        conn.execute(
            "INSERT INTO user_ratings (user_id, media_type, tmdb_id, level, title, updated_at)"
            " VALUES (?, 'movie', ?, ?, 'x', '2026-01-01')",
            (user, tmdb_id, level),
        )
    for user, tmdb_id in ((guest, 12), (other, 11)):
        conn.execute(
            "INSERT INTO user_seen_titles (user_id, media_type, tmdb_id, title, seen_at)"
            " VALUES (?, 'movie', ?, 'x', '2026-01-01')",
            (user, tmdb_id),
        )
    conn.commit()

    def item(title, tmdb_id, ref, state="ready"):
        return strip.StripItem(
            key=title, title=title, kind="movie", state=state, tmdb_id=tmdb_id, ref=ref,
            ready_seconds=50 if state == "ready" else None,
        )  # fmt: skip

    held = strip.Shared(clock=lambda: 0.0)
    items = (
        item("Ember Coast", 11, "tmdb-movie-11"),
        item("Dry Harbour", 12, "movie-900"),
        item("On its way", 13, "tmdb-movie-13", "downloading"),
        item("Unknown", None, None),
    )
    held.get(lambda _before: strip.Strip(items))
    monkeypatch.setattr(strip, "shared", held)

    ember, harbour, coming, unknown = _get(viewer).json()["items"]
    said = ("tmdb_ref", "level", "watched", "mark_kind", "mark_key")
    assert [ember[k] for k in said] == ["tmdb-movie-11", 2, False, None, None]
    assert [harbour[k] for k in said] == ["tmdb-movie-12", None, True, "movie", "900"]
    assert coming["tmdb_ref"] == "tmdb-movie-13"
    assert not {"level", "watched"} & {*coming, *unknown}


def test_the_downloads_page_s_poll_tells_the_ring_and_asks_the_services_nothing_more(
    services, viewer
):
    radarr, sonarr, _ = services
    told = viewer.get("/api/downloads").json()["strip"]
    asked = (radarr.get_queue.call_count, radarr.get_movies.call_count)
    assert asked[0] == 1
    assert told["summary"] == {"total": 2, "active": 2, "waiting": 0, "ready": 0, "percent": 40}
    # Who and what they are the page knows already; a viewer is told no more by this.
    assert told["items"] == []
    # As prompt as the page: the next poll has what it found, with no held answer before it.
    _queue(services, [_movie(11, "Ember Coast")])
    again = viewer.get("/api/downloads").json()
    assert again["strip"]["summary"]["total"] == 1
    assert radarr.get_queue.call_count == 2
    assert _get(viewer).json()["summary"] == again["strip"]["summary"]


def test_a_service_that_stops_answering_leaves_the_ring_on_downloads_what_it_shows(
    services, viewer
):
    viewer.get("/api/downloads")
    services[0].get_queue.side_effect = requests.ConnectionError("down")
    after = viewer.get("/api/downloads").json()
    assert after["strip"]["summary"]["total"] == len(after["queue"]) + bool(after["hero"])
    assert after["strip"]["outage"] == "Downloads aren't available"
