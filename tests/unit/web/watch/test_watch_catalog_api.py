"""Watch catalog JSON: TMDB search of titles not in the library, the discover rows, a
title's detail and a season's episodes, for viewers and admins alike."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.watch_catalog import _arr, _discover, _library
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes import search as search_routes
from mediaman.web.routes.recommended import api as recommended_api
from mediaman.web.routes.search import download as search_download
from mediaman.web.routes.watch import catalog_api, pages
from tests.helpers.factories import (
    insert_admin_user,
    insert_media_item,
    insert_scheduled_action,
    insert_settings,
    insert_suggestion,
)
from tests.unit.web.watch.test_watch_pages import FakePlex

ADMIN_KEYS = {"size", "size_bytes", "deletion", "actions"}


class DictCache:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    def get_or_fetch(self, key, fresh_s, stale_s, fetch, *, allow_fetch=None):
        if key in self.store:
            return self.store[key]
        if allow_fetch is not None and not allow_fetch():
            raise disk_cache.Refused(key)
        self.store[key] = fetch()
        return self.store[key]


class FakeTmdb:
    def __init__(self) -> None:
        self.lists = {
            "trending": [
                {"id": 11, "media_type": "movie", "title": "The Glasshouse"},
                {"id": 12, "media_type": "movie", "title": "Brand New"},
                {"id": 3, "media_type": "person", "name": "Somebody"},
            ],
            "popular_movies": [{"id": 12, "title": "Brand New", "poster_path": "/p.jpg"}],
            "popular_tv": [{"id": 77, "name": "Glass Coast"}],
        }
        self.search_results = [
            {"id": 11, "media_type": "movie", "title": "The Glasshouse"},
            {"id": 12, "media_type": "movie", "title": "Glass Onion"},
        ]
        self.detail: dict[int, dict] = {
            12: {
                "id": 12,
                "title": "Glass Onion",
                "release_date": "2022-11-23",
                "overview": "A mystery.",
                "vote_average": 7.14,
                "credits": {"crew": [{"job": "Director", "name": "A. Director"}], "cast": []},
                "videos": {
                    "results": [{"site": "YouTube", "type": "Trailer", "key": "abcdefghijk"}]
                },
            },
            11: {"id": 11, "title": "The Glasshouse", "overview": "From TMDB."},
            77: {
                "id": 77,
                "name": "Far Shore",
                "created_by": [{"name": "A. Creator"}],
                "seasons": [
                    {"season_number": 0, "name": "Specials"},
                    {"season_number": 1, "episode_count": 8},
                    {"season_number": 2, "episode_count": 6},
                ],
            },
        }
        self.failing = False
        self.tvdb_to_tmdb: dict[int, int] = {}

    def _list(self, name):
        return [] if self.failing else list(self.lists[name])

    def trending(self, page=1):
        return self._list("trending")

    def popular_movies(self, page=1):
        return self._list("popular_movies")

    def popular_tv(self, page=1):
        return self._list("popular_tv")

    def _get(self, path, params):
        if self.failing:
            raise SafeHTTPError(status_code=500, body_snippet="", url="https://tmdb.invalid")
        return {"results": self.search_results}

    def details(self, media_type, tmdb_id):
        return None if self.failing else self.detail.get(tmdb_id)

    def find_by_tvdb(self, tvdb_id):
        return self.tvdb_to_tmdb.get(tvdb_id)


class FakeArr:
    """Radarr and Sonarr as the state builders read them; *down* makes them unreachable."""

    def __init__(self) -> None:
        self.movies: list[dict] = []
        self.series: list[dict] = []
        self.queue: list[dict] = []
        self.down = False

    def _check(self):
        if self.down:
            raise SafeHTTPError(status_code=503, body_snippet="", url="http://arr.invalid")

    def get_movies(self):
        self._check()
        return list(self.movies)

    def get_series(self):
        self._check()
        return list(self.series)

    def get_queue(self):
        self._check()
        return list(self.queue)

    def get_movie_by_tmdb(self, tmdb_id):
        return None

    def add_movie(self, tmdb_id, title):
        self.movies.append({"tmdbId": tmdb_id, "title": title, "hasFile": False, "monitored": True})


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch):
    catalog.invalidate()
    monkeypatch.setattr(_library, "_tvdb", {})
    monkeypatch.setattr(_library, "_held", {})
    monkeypatch.setattr(_library, "sync", True)
    yield
    catalog.invalidate()


@pytest.fixture
def tmdb(monkeypatch):
    fake = FakeTmdb()
    monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: fake)
    monkeypatch.setattr(_discover, "_cache", lambda: DictCache())
    monkeypatch.setattr(catalog_api, "fetch_ratings", lambda *_a, **_k: {})
    return fake


@pytest.fixture
def no_tmdb(monkeypatch):
    monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: None)


@pytest.fixture
def arr(monkeypatch):
    fake = FakeArr()
    for module in (_arr.arr_build, search_download, recommended_api):
        monkeypatch.setattr(module, "build_radarr_from_db", lambda *_a: fake)
        monkeypatch.setattr(module, "build_sonarr_from_db", lambda *_a: fake)
    return fake


@pytest.fixture
def plex(monkeypatch):
    fake = FakePlex()
    fake.add_movie("100", "The Glasshouse")
    fake.add_show("500", "Glass Coast", {1: ("501", ["511", "512"]), 2: ("502", ["521"])})
    monkeypatch.setattr(pages, "PlexWatch", lambda *_a, **_k: fake)
    monkeypatch.setattr(pages, "_next_episode_cache", {})
    return fake


def _login(app_factory, conn, role):
    insert_admin_user(conn, username=role, role=role)
    app = app_factory(catalog_api.router, search_routes.router, recommended_api.router, conn=conn)
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, role))
    return client


@pytest.fixture
def library(conn):
    insert_settings(conn, plex_url="http://plex.invalid:32400", plex_token="tok")
    insert_settings(conn, plex_libraries='["1"]')
    insert_media_item(conn, id="100", plex_rating_key="100", title="The Glasshouse")
    for season, key in ((1, "501"), (2, "502")):
        insert_media_item(
            conn,
            id=key,
            plex_rating_key=key,
            title=f"Season {season}",
            show_title="Glass Coast",
            media_type="tv_season",
            season_number=season,
            show_rating_key="500",
        )
    insert_media_item(conn, id="900", plex_rating_key="900", title="Hidden", plex_library_id=9)
    conn.execute("UPDATE media_items SET tmdb_id = 11 WHERE id = '100'")
    conn.execute("UPDATE media_items SET tvdb_id = 5500 WHERE show_rating_key = '500'")
    conn.commit()
    return conn


@pytest.fixture
def viewer(app_factory, library):
    return _login(app_factory, library, "viewer")


@pytest.fixture
def admin(app_factory, library):
    return _login(app_factory, library, "admin")


class TestSearchTmdb:
    def test_without_tmdb_it_says_so_and_is_not_an_error(self, viewer, no_tmdb, arr):
        resp = viewer.get("/api/watch/search/tmdb", params={"q": "glass"})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "tmdb_configured": False, "items": []}

    def test_titles_the_library_holds_are_left_out(self, viewer, tmdb, arr):
        items = viewer.get("/api/watch/search/tmdb", params={"q": "glass"}).json()["items"]

        assert [(i["ref"], i["title"], i["in_library"]) for i in items] == [
            ("tmdb-movie-12", "Glass Onion", False)
        ]

    def test_a_one_letter_query_asks_tmdb_nothing(self, viewer, tmdb, arr):
        tmdb.failing = True

        resp = viewer.get("/api/watch/search/tmdb", params={"q": "g"})

        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_a_tmdb_failure_is_502(self, viewer, tmdb, arr):
        tmdb.failing = True

        resp = viewer.get("/api/watch/search/tmdb", params={"q": "glass"})

        assert resp.status_code == 502
        assert resp.json()["error"] == "tmdb_request_failed"

    def test_unreachable_arr_still_answers_without_download_state(self, viewer, tmdb, arr):
        arr.down = True

        resp = viewer.get("/api/watch/search/tmdb", params={"q": "glass"})

        assert resp.status_code == 200
        assert [i["download_state"] for i in resp.json()["items"]] == [None]

    def test_past_the_limit_only_a_search_tmdb_must_answer_is_refused(
        self, viewer, tmdb, arr, monkeypatch
    ):
        cache = DictCache()
        monkeypatch.setattr(_discover, "_cache", lambda: cache)
        monkeypatch.setattr(catalog_api, "_QUERY_LIMITER", ActionRateLimiter(1, 60))

        codes = [
            viewer.get("/api/watch/search/tmdb", params={"q": q}).status_code
            for q in ("glass", "glass", "onion")
        ]

        assert codes == [200, 200, 429]


class TestDiscover:
    def test_without_tmdb_there_are_no_rows_and_no_error(self, viewer, no_tmdb, arr):
        resp = viewer.get("/api/watch/discover")

        assert resp.status_code == 200
        assert resp.json()["tmdb_configured"] is False
        assert resp.json()["rows"] == []

    def test_rows_mark_what_the_library_holds(self, viewer, tmdb, arr):
        arr.series = [{"tmdbId": 77, "tvdbId": 5500, "seasons": []}]

        rows = {r["id"]: r for r in viewer.get("/api/watch/discover").json()["rows"]}

        assert list(rows) == ["trending", "popular_movies", "popular_tv"]
        trending = rows["trending"]["items"]
        assert [(c["ref"], c["library_ref"], c["href"]) for c in trending] == [
            ("tmdb-movie-11", "movie-100", "/watch/movie/100"),
            ("tmdb-movie-12", None, None),
        ]
        assert rows["popular_movies"]["items"][0]["poster_url"] == "/api/watch/img/tmdb/w342/p.jpg"
        assert rows["popular_tv"]["items"][0]["library_ref"] == "show-500"

    def test_a_movie_scanned_since_shows_once_the_held_library_is_out_of_date(
        self, viewer, library, tmdb, arr, monkeypatch
    ):
        now = [1000.0]
        monkeypatch.setattr(_library, "_clock", lambda: now[0])

        def brand_new_ref():
            rows = viewer.get("/api/watch/discover").json()["rows"]
            return rows[1]["items"][0]["library_ref"]

        assert brand_new_ref() is None
        insert_media_item(library, id="120", plex_rating_key="120", title="Brand New")
        library.execute("UPDATE media_items SET tmdb_id = 12 WHERE id = '120'")
        library.commit()
        now[0] += _library.INDEX_TTL_S - 1
        assert brand_new_ref() is None

        now[0] += 2
        assert brand_new_ref() == "movie-120"

    def test_unreachable_arr_still_answers_without_download_state(self, viewer, tmdb, arr):
        arr.down = True

        resp = viewer.get("/api/watch/discover")

        assert resp.status_code == 200
        assert {c["download_state"] for r in resp.json()["rows"] for c in r["items"]} == {None}

    def test_tmdb_failing_with_nothing_cached_is_502(self, viewer, tmdb, arr):
        tmdb.failing = True

        assert viewer.get("/api/watch/discover").status_code == 502


class TestTitleRefs:
    @pytest.mark.parametrize(
        "ref",
        ["movie-abc", "movie-", "tmdb-person-1", "rec-1x", "show-1-2", "movie-1234567890123456789"],
    )
    def test_a_malformed_ref_is_404(self, viewer, tmdb, arr, ref):
        assert viewer.get(f"/api/watch/title/{ref}").status_code == 404

    def test_a_library_title_outside_the_scanned_libraries_is_404(self, viewer, tmdb, arr, plex):
        assert viewer.get("/api/watch/title/movie-900").status_code == 404

    @pytest.mark.parametrize("ref", ["movie-100", "show-500"])
    def test_a_title_plex_places_outside_the_scanned_libraries_is_404(
        self, viewer, no_tmdb, arr, plex, ref
    ):
        plex.items["100"] = replace(plex.items["100"], library_section_id="9")
        plex.shows["500"] = replace(plex.shows["500"], library_section_id="9")

        assert viewer.get(f"/api/watch/title/{ref}").status_code == 404

    def test_a_missing_recommendation_is_404(self, viewer, tmdb, arr):
        assert viewer.get("/api/watch/title/rec-999").status_code == 404

    @pytest.mark.parametrize("ref", ["tmdb-movie-12", "rec-1"])
    def test_a_tmdb_title_without_tmdb_set_up_is_503(self, viewer, no_tmdb, arr, ref):
        resp = viewer.get(f"/api/watch/title/{ref}")

        assert resp.status_code == 503
        assert resp.json()["error"] == "tmdb_not_configured"


class TestLibraryTitle:
    def test_a_movie_is_built_from_plex_alone_without_tmdb(self, viewer, no_tmdb, arr, plex):
        body = viewer.get("/api/watch/title/movie-100").json()

        assert body["tmdb_configured"] is False
        assert (body["title"], body["year"], body["overview"]) == (
            "The Glasshouse",
            2019,
            "A film.",
        )
        assert body["play"]["href"].startswith("/watch/play/100")
        assert body["cast"] == []
        assert body["download"] is None

    def test_a_show_lists_its_seasons_and_episodes_from_plex(self, viewer, no_tmdb, arr, plex):
        body = viewer.get("/api/watch/title/show-500").json()

        assert [(s["number"], s["selected"]) for s in body["seasons"]] == [(1, True), (2, False)]
        assert [e["number"] for e in body["episodes"]] == [1, 2]
        assert body["play"]["label"].startswith("Play")

    def test_a_viewer_gets_no_admin_facts_and_no_keep_on_a_title_not_leaving(
        self, viewer, tmdb, arr, plex
    ):
        body = viewer.get("/api/watch/title/movie-100").json()

        assert not ADMIN_KEYS & body.keys()
        assert body["keep_allowed"] is False

    def test_a_viewer_may_keep_a_title_that_is_leaving(self, viewer, library, tmdb, arr, plex):
        insert_scheduled_action(library, media_item_id="100")

        body = viewer.get("/api/watch/title/movie-100").json()

        assert body["leaving"]["keep_id"] == "100"
        assert body["keep_allowed"] is True

    def test_an_admin_may_keep_any_library_title_and_gets_no_admin_facts_here(
        self, admin, tmdb, arr, plex
    ):
        body = admin.get("/api/watch/title/show-500").json()

        assert body["keep_allowed"] is True
        assert not ADMIN_KEYS & body.keys()

    def test_a_season_answers_its_own_episodes(self, viewer, no_tmdb, arr, plex):
        resp = viewer.get("/api/watch/title/show-500/season/2")

        assert resp.status_code == 200
        assert [e["title"] for e in resp.json()["episodes"]] == ["Episode 521"]

    @pytest.mark.parametrize(
        "path", ["/api/watch/title/movie-100/season/1", "/api/watch/title/show-500/season/9"]
    )
    def test_a_season_of_a_movie_or_a_missing_season_is_404(self, viewer, no_tmdb, arr, plex, path):
        assert viewer.get(path).status_code == 404


class TestTmdbTitle:
    def test_detail_carries_director_ratings_and_a_checked_trailer(
        self, viewer, tmdb, arr, monkeypatch
    ):
        monkeypatch.setattr(
            catalog_api,
            "fetch_ratings",
            lambda *_a, **_k: {"imdb": "7.1", "rt": "92%", "metascore": "81"},
        )

        body = viewer.get("/api/watch/title/tmdb-movie-12").json()

        assert body["director"] == "A. Director"
        assert body["ratings"] == {"imdb": "7.1", "rt": "92%", "tmdb": 7.1, "metascore": "81"}
        assert body["trailer_key"] == "abcdefghijk"
        assert body["download"]["request"]["url"] == "/api/search/download"

    def test_a_trailer_key_that_is_not_a_youtube_id_is_dropped(self, viewer, tmdb, arr):
        tmdb.detail[12]["videos"]["results"][0]["key"] = "javascript:x"

        assert viewer.get("/api/watch/title/tmdb-movie-12").json()["trailer_key"] is None

    def test_a_title_the_library_holds_names_its_library_ref(self, viewer, tmdb, arr):
        body = viewer.get("/api/watch/title/tmdb-movie-11").json()

        assert (body["in_library"], body["library_ref"], body["download"]) == (
            True,
            "movie-100",
            None,
        )

    def test_a_show_sonarr_already_tracks_says_so(self, viewer, tmdb, arr):
        arr.series = [
            {
                "tmdbId": 77,
                "tvdbId": 1,
                "seasons": [{"seasonNumber": 1, "statistics": {"episodeFileCount": 3}}],
            }
        ]

        body = viewer.get("/api/watch/title/tmdb-tv-77").json()

        assert body["sonarr_tracked"] is True
        assert body["director"] == "A. Creator"
        assert [(s["number"], s["in_library"]) for s in body["seasons"]] == [(1, True), (2, False)]

    def test_unreachable_arr_still_answers_without_download_state(self, viewer, tmdb, arr):
        arr.down = True

        resp = viewer.get("/api/watch/title/tmdb-movie-12")

        assert resp.status_code == 200
        assert resp.json()["download"]["state"] is None

    def test_a_tmdb_failure_is_502(self, viewer, tmdb, arr):
        tmdb.failing = True

        assert viewer.get("/api/watch/title/tmdb-movie-12").status_code == 502

    def test_a_recommendation_downloads_through_its_own_route(self, viewer, library, tmdb, arr):
        rec_id = insert_suggestion(library, title="Glass Onion", tmdb_id=12, reason="Because.")

        body = viewer.get(f"/api/watch/title/rec-{rec_id}").json()

        assert body["reason"] == "Because."
        assert body["download"]["request"] == {"url": f"/api/recommended/{rec_id}/download"}

    def test_a_pick_without_a_tmdb_id_answers_its_own_text_as_it_was_stored(
        self, viewer, library, tmdb, arr
    ):
        title, description = "<!--<script>", "Fish & chips</script><b>bold</b>"
        rec_id = insert_suggestion(library, title=title, description=description, tmdb_id=None)

        body = viewer.get(f"/api/watch/title/rec-{rec_id}").json()

        assert (body["title"], body["overview"]) == (title, description)

    def test_past_the_limit_only_a_title_tmdb_must_answer_is_refused(
        self, viewer, tmdb, arr, monkeypatch
    ):
        cache = DictCache()
        monkeypatch.setattr(_discover, "_cache", lambda: cache)
        monkeypatch.setattr(catalog_api, "_QUERY_LIMITER", ActionRateLimiter(1, 60))

        codes = [
            viewer.get(f"/api/watch/title/{ref}").status_code
            for ref in ("tmdb-movie-12", "tmdb-movie-12", "tmdb-movie-11")
        ]

        assert codes == [200, 200, 429]

    def test_past_the_limit_omdb_is_not_asked_again_for_a_cached_title(
        self, viewer, tmdb, arr, monkeypatch
    ):
        cache = DictCache()
        monkeypatch.setattr(_discover, "_cache", lambda: cache)
        monkeypatch.setattr(catalog_api, "_QUERY_LIMITER", ActionRateLimiter(1, 60))
        asked = []
        monkeypatch.setattr(catalog_api, "fetch_ratings", lambda *_a, **_k: asked.append(1) or {})

        for _ in range(2):
            assert viewer.get("/api/watch/title/tmdb-movie-12").status_code == 200

        assert len(asked) == 1


class TestShowsSonarrDoesNotTrack:
    """The library show "Glass Coast" (TVDB 5500) with Sonarr holding nothing."""

    def test_search_leaves_out_a_show_by_the_tmdb_id_its_rows_carry(
        self, viewer, library, tmdb, arr
    ):
        library.execute("UPDATE media_items SET tmdb_id = 77 WHERE id = '501'")
        library.commit()
        tmdb.search_results = [{"id": 77, "media_type": "tv", "name": "Renamed"}]

        assert viewer.get("/api/watch/search/tmdb", params={"q": "glass"}).json()["items"] == []

    def test_discover_marks_a_show_once_tmdb_has_mapped_its_tvdb_id(self, viewer, tmdb, arr):
        tmdb.tvdb_to_tmdb = {5500: 77}

        viewer.get("/api/watch/discover")
        rows = {r["id"]: r for r in viewer.get("/api/watch/discover").json()["rows"]}

        assert rows["popular_tv"]["items"][0]["library_ref"] == "show-500"

    def test_title_detail_matches_a_show_by_the_tvdb_id_tmdb_gives_it(self, viewer, tmdb, arr):
        tmdb.detail[77]["external_ids"] = {"tvdb_id": 5500}

        body = viewer.get("/api/watch/title/tmdb-tv-77").json()

        assert (body["in_library"], body["library_ref"], body["download"]) == (
            True,
            "show-500",
            None,
        )

    def test_the_library_title_of_a_show_takes_tmdb_details_by_its_own_tmdb_id(
        self, viewer, library, tmdb, arr, plex
    ):
        library.execute("UPDATE media_items SET tmdb_id = 77 WHERE id = '502'")
        library.commit()

        assert viewer.get("/api/watch/title/show-500").json()["director"] == "A. Creator"

    def test_search_matches_a_show_on_title_and_folder_year_and_not_one_of_another_year(
        self, viewer, library, tmdb, arr
    ):
        library.execute(
            "UPDATE media_items SET file_path = '/tv/Glass Coast (2020)/Season 01'"
            " WHERE show_rating_key = '500'"
        )
        library.commit()
        tmdb.search_results = [
            {"id": 78, "media_type": "tv", "name": "Glass Coast", "first_air_date": "2020-05-01"},
            {"id": 79, "media_type": "tv", "name": "Glass Coast", "first_air_date": "1990-05-01"},
        ]

        items = viewer.get("/api/watch/search/tmdb", params={"q": "glass"}).json()["items"]

        assert [i["ref"] for i in items] == ["tmdb-tv-79"]


class TestTitleDownload:
    def test_a_download_in_progress_carries_its_percent(self, viewer, tmdb, arr):
        arr.movies = [{"tmdbId": 12, "hasFile": False, "monitored": True}]
        arr.queue = [{"movie": {"tmdbId": 12}, "size": 100, "sizeleft": 58}]

        download = viewer.get("/api/watch/title/tmdb-movie-12").json()["download"]

        assert (download["state"], download["pct"], download["answered"]) == (
            "downloading",
            42,
            True,
        )

    def test_a_title_radarr_does_not_hold_is_answered_with_no_state(self, viewer, tmdb, arr):
        download = viewer.get("/api/watch/title/tmdb-movie-12").json()["download"]

        assert (download["state"], download["pct"], download["answered"]) == (None, None, True)

    def test_an_unreachable_arr_is_not_answered(self, viewer, tmdb, arr):
        arr.down = True

        resp = viewer.get("/api/watch/title/tmdb-tv-77")

        assert resp.status_code == 200
        download = resp.json()["download"]
        assert (download["state"], download["pct"], download["answered"]) == (None, None, False)


class TestDownloadForgetsHeldArrState:
    def test_a_search_download_shows_queued_at_once(self, admin, tmdb, arr):
        assert admin.get("/api/watch/title/tmdb-movie-12").json()["download"]["state"] is None

        resp = admin.post(
            "/api/search/download",
            json={"media_type": "movie", "tmdb_id": 12, "title": "Glass Onion"},
        )
        assert resp.status_code == 200

        assert admin.get("/api/watch/title/tmdb-movie-12").json()["download"]["state"] == "queued"

    def test_a_recommendation_download_shows_queued_at_once(self, admin, library, tmdb, arr):
        rec_id = insert_suggestion(library, title="Glass Onion", tmdb_id=12)
        ref = f"/api/watch/title/rec-{rec_id}"
        assert admin.get(ref).json()["download"]["state"] is None

        assert admin.post(f"/api/recommended/{rec_id}/download").json()["ok"] is True

        assert admin.get(ref).json()["download"]["state"] == "queued"
