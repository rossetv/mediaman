"""Watch collection JSON: the films a film's collection holds, and which may be asked for."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient

from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.watch_catalog import _arr, _collection, _library
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.watch import catalog_api, pages
from tests.helpers.factories import (
    insert_admin_user,
    insert_media_item,
    insert_settings,
    insert_suggestion,
)
from tests.unit.web.watch.test_watch_pages import FakePlex

NOW = datetime(2026, 6, 15, 12, tzinfo=UTC)
NO_ANSWER = [429, 500, requests.ConnectTimeout("slow"), ValueError("html")]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    catalog.invalidate()
    catalog.gate.reset()
    disk_cache.init(tmp_path)
    monkeypatch.setattr(disk_cache, "_failed", {})
    monkeypatch.setattr(disk_cache, "sync", True)
    monkeypatch.setattr(_library, "_held", {})
    monkeypatch.setattr(_library, "sync", True)
    monkeypatch.setattr(catalog_api, "now_utc", lambda: NOW)
    monkeypatch.setattr(catalog_api, "_COLLECTION_LIMITER", ActionRateLimiter(60, 60))
    queue = [{"tmdbId": 13, "hasFile": False, "monitored": True}]
    radarr = SimpleNamespace(get_movies=lambda: queue, get_queue=list)
    monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", lambda *_: radarr)
    monkeypatch.setattr(_arr.arr_build, "build_sonarr_from_db", lambda *_: None)
    yield
    catalog.invalidate()
    catalog.gate.reset()


@pytest.fixture
def plex(monkeypatch):
    fake = FakePlex()
    fake.add_movie("100", "Film 11")
    monkeypatch.setattr(pages, "PlexWatch", lambda *_a, **_k: fake)
    return fake


@pytest.fixture
def viewer(app_factory, conn, plex):
    insert_settings(conn, plex_url="http://plex.invalid:32400", plex_token="tok")
    insert_settings(conn, plex_libraries='["1"]')
    insert_media_item(conn, id="100", plex_rating_key="100", title="Film 11")
    conn.execute("UPDATE media_items SET tmdb_id = 11 WHERE id = '100'")
    insert_media_item(conn, id="101", plex_rating_key="101", title="No id")
    insert_media_item(conn, id="900", plex_rating_key="900", title="Hid", plex_library_id=9)
    conn.execute("UPDATE media_items SET tmdb_id = 12 WHERE id = '900'")
    conn.commit()
    insert_admin_user(conn, username="viewer", role="viewer")
    client = TestClient(app_factory(catalog_api.router, conn=conn))
    client.cookies.set("session_token", create_session(conn, "viewer"))
    return client


def _part(film_id, released="2001-01-01", **more):
    return {"id": film_id, "title": f"Film {film_id}", "release_date": released, **more}


def _film(film_id, collection: object = 77):
    return {
        "id": film_id,
        "title": f"Film {film_id}",
        "belongs_to_collection": {"id": collection} if collection else None,
    }


@pytest.fixture
def tmdb(monkeypatch):
    """TMDB's answers by path; a status code or an exception fails."""
    answers: dict[str, object] = {f"/movie/{i}": _film(i) for i in (11, 12, 13)}
    answers["/collection/77"] = {"name": "The Films", "parts": [_part(11), _part(12), _part(13)]}

    def get(self, path, params=None):
        answer = answers[path]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            raise SafeHTTPError(status_code=answer, body_snippet="", url="https://tmdb.invalid")
        return answer

    monkeypatch.setattr(TmdbClient, "_get", get)
    monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: TmdbClient("token"))
    return answers


def _ask(client, ref="movie-100"):
    resp = client.get(f"/api/watch/title/{ref}/collection")
    assert resp.status_code == 200
    return resp.json()


def test_films_come_in_the_order_of_release_with_those_undated_last(viewer, tmdb):
    tmdb["/collection/77"] = {
        "name": "The Films",
        "parts": [_part(15, ""), _part(12, "2004-05-01"), _part(14, None), _part(11, "1999-01-01")],
    }

    body = _ask(viewer)

    assert body["name"] == "The Films"
    assert [(f["ref"], f["year"], f["release_date"]) for f in body["films"]] == [
        ("tmdb-movie-11", 1999, "1999-01-01"),
        ("tmdb-movie-12", 2004, "2004-05-01"),
        ("tmdb-movie-15", None, None),
        ("tmdb-movie-14", None, None),
    ]


def test_each_film_says_whether_it_may_be_asked_for_and_why_not(viewer, tmdb):
    tmdb["/collection/77"]["parts"] += [
        _part(14, "2026-06-15"),
        _part(15, "2026-06-16"),
        _part(16, "soon"),
    ]

    films = _ask(viewer)["films"]

    assert {f["ref"]: (f["can_request"], f["why_not"]) for f in films} == {
        "tmdb-movie-11": (False, "held"),
        "tmdb-movie-12": (True, None),
        "tmdb-movie-13": (False, "requested"),
        "tmdb-movie-14": (True, None),
        "tmdb-movie-15": (False, "unreleased"),
        "tmdb-movie-16": (False, "undated"),
    }


def test_a_film_radarr_holds_and_plex_does_not_yet_is_held(viewer, tmdb, monkeypatch):
    movies = [{"tmdbId": 12, "hasFile": True, "monitored": True}]
    radarr = SimpleNamespace(get_movies=lambda: movies, get_queue=list)
    monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", lambda *_: radarr)

    assert [f["why_not"] for f in _ask(viewer)["films"]][:2] == ["held", "held"]


def test_nothing_may_be_asked_for_while_radarr_does_not_answer(viewer, tmdb, monkeypatch):
    monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", lambda *_: None)

    assert [f["why_not"] for f in _ask(viewer)["films"]] == ["held", "unavailable", "unavailable"]


def test_a_film_is_the_card_discover_gives_it(viewer, tmdb):
    tmdb["/trending/all/week"] = {"results": [{**_part(i), "media_type": "movie"} for i in (11,)]}
    tmdb["/movie/popular"] = tmdb["/tv/popular"] = 500

    film = _ask(viewer)["films"][0]
    [card] = viewer.get("/api/watch/discover").json()["rows"][0]["items"]

    assert card["library_ref"] == "movie-100"
    assert {k: film[k] for k in card} == card


@pytest.mark.parametrize("kind", ["movie", "tmdb", "rec", "pick"])
def test_every_form_of_ref_marks_its_own_film(viewer, tmdb, conn, kind):
    if kind == "movie":
        ref, film = "movie-100", 11
    elif kind == "tmdb":
        ref, film = "tmdb-movie-13", 13
    elif kind == "rec":
        ref, film = f"rec-{insert_suggestion(conn, tmdb_id=12)}", 12
    else:
        pick = conn.execute(
            "INSERT INTO user_picks (username, title, media_type, tmdb_id, batch_id, created_at)"
            " VALUES ('viewer', 'Film 12', 'movie', 12, '2026-06-01', ?)",
            (NOW.isoformat(),),
        ).lastrowid
        ref, film = f"pick-{pick}", 12
    conn.commit()

    here = [f["ref"] for f in _ask(viewer, ref)["films"] if f["here"]]

    assert here == [f"tmdb-movie-{film}"]


def test_there_is_nothing_for_a_title_not_listed_or_without_films_beside_it(viewer, tmdb, conn):
    tv = insert_suggestion(conn, media_type="tv", tmdb_id=11)
    insert_admin_user(conn, username="viewer2", role="viewer")
    other = conn.execute(
        "INSERT INTO user_picks (username, title, media_type, tmdb_id, batch_id, created_at)"
        " VALUES ('viewer2', 'Film 12', 'movie', 12, '2026-06-01', ?)",
        (NOW.isoformat(),),
    ).lastrowid
    conn.commit()
    tmdb["/movie/20"] = _film(20, None)
    refs = ["movie-101", "movie-900", "movie-404", "show-5", "tmdb-tv-11", "tmdb-movie-20"]

    assert [_ask(viewer, ref) for ref in [*refs, f"rec-{tv}", f"pick-{other}"]] == [None] * 8


def test_a_film_plex_places_outside_the_libraries_has_none(viewer, tmdb, plex, monkeypatch):
    monkeypatch.setattr(catalog_api, "is_in_libraries", lambda *_: False)

    assert _ask(viewer) is None


def test_a_film_tmdb_marks_adult_is_left_out_and_may_leave_too_few(viewer, tmdb):
    tmdb["/collection/77"]["parts"][1]["adult"] = True
    assert [f["ref"] for f in _ask(viewer)["films"]] == ["tmdb-movie-11", "tmdb-movie-13"]

    tmdb["/movie/12"] = {**_film(12), "adult": True}
    assert _ask(viewer, "tmdb-movie-12") is None

    tmdb["/movie/20"] = _film(20, 78)
    tmdb["/collection/78"] = {"name": "Two", "parts": [_part(20), _part(21, adult=True)]}
    assert _ask(viewer, "tmdb-movie-20") is None


def test_a_malformed_ref_is_404(viewer, tmdb):
    assert viewer.get("/api/watch/title/nonsense/collection").status_code == 404


def test_without_a_sign_in_it_is_refused(viewer, tmdb):
    viewer.cookies.clear()

    assert viewer.get("/api/watch/title/movie-100/collection").status_code == 401


def test_without_a_tmdb_key_there_is_nothing(viewer, monkeypatch):
    monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: None)

    assert _ask(viewer) is None


@pytest.mark.parametrize(
    ("path", "failure"),
    [(path, failure) for path in ("/movie/11", "/collection/77") for failure in NO_ANSWER]
    + [
        ("/collection/77", {"name": None, "parts": [_part(11), _part(12)]}),
        ("/collection/77", {"name": "The Films", "parts": None}),
    ],
    ids=str,
)
def test_a_failure_of_tmdb_says_so_and_is_not_kept(viewer, tmdb, path, failure):
    answer, tmdb[path] = tmdb[path], failure

    resp = viewer.get("/api/watch/title/movie-100/collection")
    assert resp.status_code == (429 if failure == 429 else 502)

    tmdb[path] = answer
    catalog.gate.reset()
    disk_cache._failed.clear()
    assert len(_ask(viewer)["films"]) == 3


def test_once_read_tmdb_is_not_asked_again(viewer, tmdb):
    held = _ask(viewer)
    tmdb["/movie/11"] = tmdb["/collection/77"] = 500

    assert _ask(viewer) == held


def test_a_collection_tmdb_has_none_for_is_asked_for_again_after_a_day(viewer, tmdb, monkeypatch):
    answer, tmdb["/collection/77"] = tmdb["/collection/77"], 404
    assert _ask(viewer) is None
    tmdb["/collection/77"] = answer
    assert _ask(viewer) is None

    later = time.time() + 86_401
    monkeypatch.setattr(_collection, "_wall", lambda: later)

    assert len(_ask(viewer)["films"]) == 3


def test_details_held_from_before_the_collection_are_read_anew_for_that_film(viewer, tmdb):
    old = catalog.tmdb_fields(_film(11), "movie")
    del old["collection_id"]
    disk_cache.meta().get_or_fetch("tmdb:details:v5:movie:11", 1, 1, lambda: old)
    asked = tmdb["/movie/11"]
    tmdb["/movie/11"] = 500

    assert viewer.get("/api/watch/title/movie-100/collection").status_code == 502
    # The title itself still shows what was held of it.
    assert catalog.details(TmdbClient("token"), "movie", 11) == old

    tmdb["/movie/11"] = asked
    catalog.gate.reset()
    disk_cache._failed.clear()
    assert len(_ask(viewer)["films"]) == 3


def test_past_the_limit_only_a_collection_tmdb_must_answer_is_refused(viewer, tmdb, monkeypatch):
    monkeypatch.setattr(catalog_api, "_COLLECTION_LIMITER", ActionRateLimiter(1, 60))
    held = _ask(viewer)

    assert _ask(viewer) == held
    assert viewer.get("/api/watch/title/tmdb-movie-12/collection").status_code == 429
