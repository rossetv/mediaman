"""Watch catalog JSON: TMDB search of titles not in the library, the discover rows, a
title's detail and a season's episodes, for viewers and admins alike."""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from mediaman.core.time import now_utc
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.plex_watch import PlexUnavailable
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.watch_catalog import _arr, _discover, _library
from mediaman.web.auth.session_store import create_session
from mediaman.web.repository.watch_progress import MarkTarget, mark_watched
from mediaman.web.routes import search as search_routes
from mediaman.web.routes.recommended import api as recommended_api
from mediaman.web.routes.search import _enrichment
from mediaman.web.routes.search import download as search_download
from mediaman.web.routes.watch import catalog_api, pages
from tests.helpers.factories import (
    insert_admin_user,
    insert_download_notification,
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

    def get_or_fetch(self, key, fresh_s, stale_s, fetch, *, allow_fetch=None, brief=False):
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

    def details_or_raise(self, media_type, tmdb_id):
        if self.failing or tmdb_id not in self.detail:
            raise SafeHTTPError(
                status_code=500 if self.failing else 404,
                body_snippet="",
                url="https://tmdb.invalid",
            )
        return self.detail[tmdb_id]

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
    monkeypatch.setattr(_enrichment, "_paused_until", 0.0)
    monkeypatch.setattr(catalog_api, "_title_paused", {})
    monkeypatch.setattr(catalog_api, "_unanswered_streak", 0)
    monkeypatch.setattr(catalog_api, "_outage_until", 0.0)
    yield
    catalog.invalidate()


@pytest.fixture
def tmdb(monkeypatch, conn):
    insert_settings(conn, omdb_api_key="omdb-key")
    fake = FakeTmdb()
    monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: fake)
    monkeypatch.setattr(_discover, "_cache", lambda: DictCache())
    monkeypatch.setattr(catalog_api, "lookup_ratings", lambda *_a, **_k: {})
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


def _mark(conn, *keys: str, item: str, show: str | None = "500", user: str = "viewer") -> None:
    mark_watched(conn, user, [MarkTarget(k, item, show, 0) for k in keys], now_utc())


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
        assert resp.json() == {"ok": True, "tmdb_configured": False, "items": [], "people": []}

    def test_titles_the_library_holds_are_left_out(self, viewer, tmdb, arr):
        items = viewer.get("/api/watch/search/tmdb", params={"q": "glass"}).json()["items"]

        assert [(i["ref"], i["title"], i["in_library"]) for i in items] == [
            ("tmdb-movie-12", "Glass Onion", False)
        ]

    def test_the_people_a_search_names_come_beside_the_titles(self, viewer, tmdb, arr):
        tmdb.search_results.append({"id": 4, "media_type": "person", "name": "Glass Marlowe"})

        body = viewer.get("/api/watch/search/tmdb", params={"q": "glass"}).json()

        assert [(p["person_id"], p["name"]) for p in body["people"]] == [(4, "Glass Marlowe")]
        assert [i["ref"] for i in body["items"]] == ["tmdb-movie-12"]

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

    def test_a_card_the_library_holds_carries_the_viewers_own_mark(
        self, viewer, library, tmdb, arr
    ):
        _mark(library, "100", item="100", show=None)

        rows = {r["id"]: r for r in viewer.get("/api/watch/discover").json()["rows"]}

        held, other = rows["trending"]["items"]
        assert (held["watched"], held["mark_kind"], held["mark_key"]) == (True, "movie", "100")
        assert (other["watched"], other["mark_kind"], other["mark_key"]) == (False, None, None)

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


class TestDiscoverPages:
    @pytest.fixture(autouse=True)
    def _limiter(self, monkeypatch):
        monkeypatch.setattr(catalog_api, "_PAGE_LIMITER", ActionRateLimiter(60, 60))

    @pytest.fixture
    def pages(self, tmdb, monkeypatch):
        """TMDB's answer to a list page, by path; a status code fails."""
        answers: dict[str, object] = {}
        asked: list[tuple[str, int]] = []
        cache = DictCache()
        monkeypatch.setattr(_discover, "_cache", lambda: cache)

        def get(path, params):
            asked.append((path, params["page"]))
            answer = answers[path]
            if isinstance(answer, int):
                raise SafeHTTPError(status_code=answer, body_snippet="", url="https://tmdb.invalid")
            return answer

        tmdb._get = get
        tmdb.asked = asked
        return answers

    def _page(self, client, row="popular_movies", page=2):
        return client.get("/api/watch/discover", params={"row": row, "page": page})

    @pytest.mark.parametrize(
        ("row", "path", "item", "ref"),
        [
            ("trending", "/trending/all/week", {"id": 5, "media_type": "tv"}, "tmdb-tv-5"),
            ("popular_movies", "/movie/popular", {"id": 5}, "tmdb-movie-5"),
            ("popular_tv", "/tv/popular", {"id": 5}, "tmdb-tv-5"),
        ],
    )
    def test_a_later_page_of_each_row_has_its_cards_and_the_page_after_it(
        self, viewer, tmdb, pages, arr, row, path, item, ref
    ):
        pages[path] = {"results": [item, {"id": 3, "media_type": "person"}], "total_pages": 9}

        body = self._page(viewer, row).json()

        assert body["items"][0]["ref"] == ref
        assert body["next_page"] == 3
        assert tmdb.asked == [(path, 2)]

    def test_a_first_load_names_the_page_to_go_on_with(self, viewer, tmdb, arr):
        rows = viewer.get("/api/watch/discover").json()["rows"]

        assert [r["next_page"] for r in rows] == [2, 2, 2]

    @pytest.mark.parametrize(
        ("page", "answer"),
        [
            (9, {"results": [{"id": 5}], "total_pages": 9}),
            (500, {"results": [{"id": 5}], "total_pages": 40000}),
            (2, {"results": [], "total_pages": 9}),
        ],
    )
    def test_the_last_page_tmdb_gives_is_the_end(self, viewer, pages, arr, page, answer):
        pages["/movie/popular"] = answer

        assert self._page(viewer, page=page).json()["next_page"] is None

    def test_a_page_held_by_the_library_is_marked(self, viewer, pages, arr):
        pages["/movie/popular"] = {"results": [{"id": 11, "title": "The Glasshouse"}]}

        assert self._page(viewer).json()["items"][0]["library_ref"] == "movie-100"

    @pytest.mark.parametrize(("row", "page", "status"), [("x", 2, 404), ("trending", 501, 422)])
    def test_an_unknown_row_or_a_page_past_tmdbs_last_is_refused(
        self, viewer, pages, arr, row, page, status
    ):
        assert self._page(viewer, row, page).status_code == status

    @pytest.mark.parametrize(("failure", "status"), [(500, 502), (429, 429)])
    def test_a_tmdb_failure_is_an_error_not_the_end_of_the_row(
        self, viewer, pages, arr, failure, status
    ):
        pages["/movie/popular"] = failure

        assert self._page(viewer).status_code == status

    def test_past_the_limit_only_a_page_tmdb_must_answer_is_refused_and_search_is_not(
        self, viewer, pages, arr, monkeypatch
    ):
        monkeypatch.setattr(catalog_api, "_PAGE_LIMITER", ActionRateLimiter(1, 60))
        pages["/movie/popular"] = {"results": [{"id": 5}], "total_pages": 9}
        pages["/search/multi"] = {"results": []}

        codes = [self._page(viewer, page=n).status_code for n in (2, 2, 3)]
        search = viewer.get("/api/watch/search/tmdb", params={"q": "glass"})

        assert (codes, search.status_code) == ([200, 200, 429], 200)

    def test_only_what_a_card_shows_is_cached(self, viewer, pages, arr):
        pages["/movie/popular"] = {
            "results": [{"id": 5, "title": "T", "overview": "x" * 900}],
            "total_pages": 9,
        }
        self._page(viewer)

        (held,) = _discover._cache().store.values()
        assert held["items"] == [{"id": 5, "title": "T", "media_type": "movie"}]


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

    def test_a_show_says_which_of_its_seasons_leave_and_when(
        self, viewer, library, no_tmdb, arr, plex
    ):
        for item, days in (("501", 9), ("502", 3)):
            insert_scheduled_action(
                library,
                media_item_id=item,
                action="scheduled_deletion",
                token=f"token-{item}",
                execute_at=(now_utc() + timedelta(days=days)).isoformat(),
            )

        body = viewer.get("/api/watch/title/show-500").json()

        assert [(s["season"], s["keep_id"]) for s in body["leaving_seasons"]] == [
            (2, "502"),
            (1, "501"),
        ]
        assert body["leaving"]["season"] == 2

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

    def test_a_movie_says_it_was_seen_even_with_plex_down(
        self, viewer, library, no_tmdb, arr, plex
    ):
        _mark(library, "100", item="100", show=None)
        insert_admin_user(library, username="cian", role="viewer")
        _mark(library, "511", "512", "521", item="501", user="cian")
        plex.error = PlexUnavailable("down")

        movie = viewer.get("/api/watch/title/movie-100").json()
        show = viewer.get("/api/watch/title/show-500").json()

        assert (movie["watched"], movie["mark_kind"], movie["mark_key"]) == (True, "movie", "100")
        assert (show["watched"], show["mark_kind"], show["mark_key"]) == (False, "show", "500")

    def test_a_show_says_which_seasons_and_episodes_were_seen(
        self, viewer, library, no_tmdb, arr, plex
    ):
        _mark(library, "521", item="502")
        _mark(library, "511", item="501")
        library.execute("UPDATE media_items SET episode_count = 1 WHERE id = '502'")
        library.commit()

        body = viewer.get("/api/watch/title/show-500").json()
        season = viewer.get("/api/watch/title/show-500/season/2").json()

        assert body["play"]["label"] == "Play S1 · E2"
        assert [(s["key"], s["watched"]) for s in body["seasons"]] == [
            ("501", False),
            ("502", True),
        ]
        assert [(e["key"], e["watched"]) for e in body["episodes"]] == [
            ("511", True),
            ("512", False),
        ]
        assert body["watched"] is False
        assert (season["key"], season["watched"]) == ("502", True)

    def test_a_show_is_seen_once_every_season_is(self, viewer, library, no_tmdb, arr, plex):
        _mark(library, "511", "512", item="501")
        _mark(library, "521", item="502")
        library.execute("UPDATE media_items SET episode_count = 1 WHERE id = '502'")
        library.commit()

        assert viewer.get("/api/watch/title/show-500").json()["watched"] is True

    def test_a_show_seen_to_the_end_is_read_without_every_seasons_list(
        self, viewer, library, no_tmdb, arr, plex, monkeypatch
    ):
        plex.add_show("500", "Glass Coast", {n: (f"50{n}", [f"5{n}1"]) for n in range(1, 4)})
        _mark(library, "511", item="501")
        _mark(library, "521", item="502")
        _mark(library, "531", item="503")
        library.execute("UPDATE media_items SET episode_count = 1 WHERE show_rating_key = '500'")
        library.commit()
        read: list[str] = []
        real = plex.get_season_episodes
        monkeypatch.setattr(plex, "get_season_episodes", lambda k: read.append(k) or real(k))

        viewer.get("/api/watch/title/show-500")

        assert "502" not in read

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
    def test_the_genres_and_cast_name_what_they_lead_to(self, viewer, tmdb, arr):
        tmdb.detail[12]["genres"] = [{"id": 27, "name": "Horror"}]
        tmdb.detail[12]["credits"] = {"cast": [{"id": 4, "name": "Glass Marlowe"}]}

        body = viewer.get("/api/watch/title/tmdb-movie-12").json()

        assert body["genre_refs"] == [{"name": "Horror", "genre": "horror"}]
        assert [c["person_id"] for c in body["cast"]] == [4]

    def test_the_director_comes_with_their_person_id(self, viewer, tmdb, arr):
        tmdb.detail[12]["credits"] = {"crew": [{"job": "Director", "id": 9, "name": "A. Director"}]}

        assert viewer.get("/api/watch/title/tmdb-movie-12").json()["director_id"] == 9
        assert viewer.get("/api/watch/title/tmdb-movie-11").json()["director_id"] is None

    def test_detail_carries_director_ratings_and_a_checked_trailer(
        self, viewer, tmdb, arr, monkeypatch
    ):
        monkeypatch.setattr(
            catalog_api,
            "lookup_ratings",
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
        assert (body["watched"], body["mark_key"]) == (False, "100")

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

    UNMONITORED_SHOW = {
        "tmdbId": 77,
        "tvdbId": 1,
        "monitored": False,
        "seasons": [
            {
                "seasonNumber": 1,
                "monitored": False,
                "statistics": {"episodeCount": 8, "episodeFileCount": 0},
            }
        ],
    }

    def test_a_show_sonarr_holds_unmonitored_is_tracked_and_not_queued(self, viewer, tmdb, arr):
        arr.series = [self.UNMONITORED_SHOW]

        body = viewer.get("/api/watch/title/tmdb-tv-77").json()

        assert body["sonarr_tracked"] is True
        assert (body["download"]["state"], body["download"]["answered"]) == (None, True)

    def test_a_pick_sonarr_holds_unmonitored_is_tracked_and_not_queued(
        self, viewer, library, tmdb, arr
    ):
        arr.series = [self.UNMONITORED_SHOW]
        rec_id = insert_suggestion(library, title="Far Shore", tmdb_id=77, media_type="tv")

        body = viewer.get(f"/api/watch/title/rec-{rec_id}").json()

        assert body["sonarr_tracked"] is True
        assert body["download"]["state"] is None

    def test_unreachable_arr_still_answers_without_download_state(self, viewer, tmdb, arr):
        arr.down = True

        resp = viewer.get("/api/watch/title/tmdb-movie-12")

        assert resp.status_code == 200
        assert resp.json()["download"]["state"] is None

    def test_an_adult_title_and_one_tmdb_has_not_are_not_found(self, viewer, conn, tmdb, arr):
        tmdb.detail[12]["adult"] = True
        rec = insert_suggestion(conn, title="Glass Onion", tmdb_id=12)

        refs = ("tmdb-movie-12", "tmdb-movie-404", f"rec-{rec}")

        assert [viewer.get(f"/api/watch/title/{r}").status_code for r in refs] == [404] * 3

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

    def test_a_pick_stored_cut_mid_sentence_ends_at_its_last_full_one(
        self, viewer, library, tmdb, arr
    ):
        whole = "A lighthouse keeper finds a door in the sea wall. "
        cut = (whole + "She goes through it " * 20)[:250]
        rec_id = insert_suggestion(library, title="Door", description=cut, tmdb_id=None)

        assert viewer.get(f"/api/watch/title/rec-{rec_id}").json()["overview"] == whole.strip()

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
        # Nothing is remembered of OMDb's answers, so only the limit can refuse the second ask.
        monkeypatch.setattr(catalog_api, "upsert_ratings_cache", lambda *_a, **_k: None)
        asked = []
        monkeypatch.setattr(catalog_api, "lookup_ratings", lambda *_a, **_k: asked.append(1) or {})

        for _ in range(2):
            assert viewer.get("/api/watch/title/tmdb-movie-12").status_code == 200

        assert len(asked) == 1

    def test_an_omdb_answer_with_no_ratings_is_remembered_for_a_day(
        self, viewer, tmdb, arr, conn, monkeypatch
    ):
        asked = []
        monkeypatch.setattr(catalog_api, "lookup_ratings", lambda *_a, **_k: asked.append(1) or {})

        for _ in range(3):
            assert viewer.get("/api/watch/title/tmdb-movie-12").status_code == 200
        assert len(asked) == 1

        stale = (now_utc() - timedelta(days=1, minutes=1)).isoformat()
        conn.execute("UPDATE ratings_cache SET fetched_at = ?", (stale,))
        conn.commit()
        viewer.get("/api/watch/title/tmdb-movie-12")

        assert len(asked) == 2

    def _hold_ratings(self, conn, fetched_at):
        conn.execute(
            "INSERT INTO ratings_cache (tmdb_id, media_type, imdb_rating, rt_rating, metascore,"
            " fetched_at) VALUES (12, 'movie', '7.1', '92%', '81', ?)",
            (fetched_at,),
        )
        conn.commit()

    def test_an_omdb_that_gives_no_answer_keeps_the_ratings_held_and_caches_nothing_new(
        self, viewer, tmdb, arr, conn, monkeypatch
    ):
        old = (now_utc() - timedelta(days=40)).isoformat()
        self._hold_ratings(conn, old)
        monkeypatch.setattr(catalog_api, "lookup_ratings", lambda *_a, **_k: None)

        body = viewer.get("/api/watch/title/tmdb-movie-12").json()

        row = conn.execute(
            "SELECT imdb_rating, rt_rating, fetched_at FROM ratings_cache"
        ).fetchone()
        assert (row["imdb_rating"], row["rt_rating"], row["fetched_at"]) == ("7.1", "92%", old)
        assert body["ratings"]["imdb"] == "7.1"

    def test_an_omdb_answer_of_no_ratings_does_not_wipe_the_ratings_held(
        self, viewer, tmdb, arr, conn, monkeypatch
    ):
        self._hold_ratings(conn, (now_utc() - timedelta(days=40)).isoformat())
        monkeypatch.setattr(catalog_api, "lookup_ratings", lambda *_a, **_k: {})

        body = viewer.get("/api/watch/title/tmdb-movie-12").json()

        row = conn.execute("SELECT imdb_rating, rt_rating, metascore FROM ratings_cache").fetchone()
        assert (row["imdb_rating"], row["rt_rating"], row["metascore"]) == ("7.1", "92%", "81")
        assert body["ratings"]["imdb"] == "7.1"
        assert body["ratings"]["rt"] == "92%"
        assert body["ratings"]["metascore"] == "81"

    def test_an_omdb_answer_with_some_ratings_adds_to_the_ratings_held(
        self, viewer, tmdb, arr, conn, monkeypatch
    ):
        self._hold_ratings(conn, (now_utc() - timedelta(days=40)).isoformat())
        monkeypatch.setattr(catalog_api, "lookup_ratings", lambda *_a, **_k: {"imdb": "8.0"})

        body = viewer.get("/api/watch/title/tmdb-movie-12").json()

        assert body["ratings"]["imdb"] == "8.0"
        assert body["ratings"]["rt"] == "92%"

    def test_a_refused_allowance_shows_the_ratings_held_and_does_not_ask_omdb(
        self, viewer, tmdb, arr, conn, monkeypatch
    ):
        cache = DictCache()
        monkeypatch.setattr(_discover, "_cache", lambda: cache)
        viewer.get("/api/watch/title/tmdb-movie-12")  # TMDB is now cached
        conn.execute("DELETE FROM ratings_cache")
        self._hold_ratings(conn, (now_utc() - timedelta(days=40)).isoformat())
        monkeypatch.setattr(catalog_api, "_QUERY_LIMITER", MagicMock(check=lambda *_a, **_k: False))
        asked = []
        monkeypatch.setattr(catalog_api, "lookup_ratings", lambda *_a, **_k: asked.append(1) or {})

        body = viewer.get("/api/watch/title/tmdb-movie-12").json()

        assert asked == []
        assert body["ratings"]["imdb"] == "7.1"

    def test_after_no_answer_a_second_open_asks_nothing_and_is_not_counted(
        self, viewer, tmdb, arr, monkeypatch
    ):
        cache = DictCache()
        monkeypatch.setattr(_discover, "_cache", lambda: cache)
        limiter = MagicMock(check=MagicMock(return_value=True))
        monkeypatch.setattr(catalog_api, "_QUERY_LIMITER", limiter)
        asked = []
        monkeypatch.setattr(
            catalog_api, "lookup_ratings", lambda *_a, **_k: asked.append(1) or None
        )

        for _ in range(2):
            assert viewer.get("/api/watch/title/tmdb-movie-12").status_code == 200

        assert len(asked) == 1
        assert limiter.check.call_count == 1

    def test_one_title_without_an_answer_does_not_pause_the_ratings_of_another(
        self, viewer, tmdb, arr, monkeypatch
    ):
        monkeypatch.setattr(
            catalog_api,
            "lookup_ratings",
            lambda title, *_a, **_k: None if title == "The Glasshouse" else {"imdb": "6.5"},
        )

        viewer.get("/api/watch/title/tmdb-movie-11")
        body = viewer.get("/api/watch/title/tmdb-movie-12").json()

        assert body["ratings"]["imdb"] == "6.5"
        assert _enrichment._paused_until == 0.0

    def test_three_titles_in_a_row_without_an_answer_pause_omdb_for_all(self, monkeypatch):
        for tmdb_id in range(3):
            catalog_api._pause_title((tmdb_id, "movie"))

        assert catalog_api._title_is_paused((99, "movie"))

    def test_an_answer_resets_the_run_of_titles_without_one(self):
        for tmdb_id in range(2):
            catalog_api._pause_title((tmdb_id, "movie"))
        catalog_api._note_answer()
        catalog_api._pause_title((5, "movie"))

        assert not catalog_api._title_is_paused((99, "movie"))

    def test_one_title_opened_three_times_at_once_does_not_pause_the_others(self):
        for _ in range(3):
            catalog_api._pause_title((1, "movie"))

        assert catalog_api._title_is_paused((1, "movie"))
        assert not catalog_api._title_is_paused((99, "movie"))

    def test_ratings_resets_the_run_of_titles_without_an_answer_when_omdb_answers(
        self, conn, monkeypatch
    ):
        asked = []

        def lookup(title, *_a, **_k):
            asked.append(title)
            return {"imdb": "7.0"} if title == "answered" else None

        monkeypatch.setattr(catalog_api, "get_omdb_key", lambda *_a: "omdb-key")
        monkeypatch.setattr(catalog_api, "is_key_refused", lambda _key: False)
        monkeypatch.setattr(catalog_api, "lookup_ratings", lookup)

        for tmdb_id, title in enumerate(["a", "b", "answered", "c", "new"], start=1):
            catalog_api._ratings(conn, "secret", "movie", tmdb_id, title, None, lambda: True)

        assert asked[-1] == "new"

    def test_the_title_pause_map_holds_up_under_threads_when_full(self):
        from concurrent.futures import ThreadPoolExecutor

        for i in range(catalog_api._TITLE_PAUSE_MAX):
            catalog_api._title_paused[(i, "movie")] = time.monotonic() + 900

        def work(n):
            for i in range(300):
                catalog_api._pause_title((n * 1000 + i, "tv"))
                catalog_api._title_is_paused((i, "movie"))

        switch = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(work, range(8)))
        finally:
            sys.setswitchinterval(switch)

        assert len(catalog_api._title_paused) <= catalog_api._TITLE_PAUSE_MAX

    def test_a_title_is_not_asked_of_omdb_while_search_has_paused_it(
        self, viewer, tmdb, arr, conn, monkeypatch
    ):
        self._hold_ratings(conn, (now_utc() - timedelta(days=40)).isoformat())
        monkeypatch.setattr(_enrichment, "_paused_until", time.monotonic() + 60)
        asked = []
        monkeypatch.setattr(catalog_api, "lookup_ratings", lambda *_a, **_k: asked.append(1) or {})

        body = viewer.get("/api/watch/title/tmdb-movie-12").json()

        assert asked == []
        assert body["ratings"]["imdb"] == "7.1"

    def test_a_title_is_not_counted_while_omdb_refuses_the_key(
        self, viewer, tmdb, arr, monkeypatch
    ):
        cache = DictCache()
        monkeypatch.setattr(_discover, "_cache", lambda: cache)
        viewer.get("/api/watch/title/tmdb-movie-11")  # TMDB is now cached
        monkeypatch.setattr(catalog_api, "is_key_refused", lambda _key: True)
        limiter = MagicMock(check=MagicMock(return_value=True))
        monkeypatch.setattr(catalog_api, "_QUERY_LIMITER", limiter)
        monkeypatch.setattr(catalog_api, "fetch_ratings_cache", lambda *_a: [])

        assert viewer.get("/api/watch/title/tmdb-movie-11").status_code == 200

        limiter.check.assert_not_called()

    def test_an_omdb_that_gives_no_answer_is_not_remembered_as_no_ratings(
        self, viewer, tmdb, arr, conn, monkeypatch
    ):
        monkeypatch.setattr(catalog_api, "lookup_ratings", lambda *_a, **_k: None)

        viewer.get("/api/watch/title/tmdb-movie-12")

        assert conn.execute("SELECT COUNT(*) FROM ratings_cache").fetchone()[0] == 0


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


class TestTitleDownloadNotify:
    """Whether this person, and only this person, is still owed an email for the title."""

    OWN = "viewer@example.com"

    @pytest.fixture
    def emailed(self, viewer, library, tmdb, arr):
        library.execute("UPDATE admin_users SET email = ? WHERE username = 'viewer'", (self.OWN,))
        library.commit()
        return viewer

    def _notify(self, client, ref):
        return client.get(f"/api/watch/title/{ref}").json()["download"]["notify"]

    def test_an_own_unsent_notification_of_a_movie(self, emailed, library):
        insert_download_notification(library, email=self.OWN.upper(), tmdb_id=12)

        assert self._notify(emailed, "tmdb-movie-12") is True

    def test_an_own_unsent_notification_of_a_pick(self, emailed, library):
        rec_id = insert_suggestion(library, title="Glass Onion", tmdb_id=12)
        insert_download_notification(library, email=self.OWN, tmdb_id=12)

        assert self._notify(emailed, f"rec-{rec_id}") is True

    def test_one_being_sent_is_still_unsent(self, emailed, library):
        insert_download_notification(library, email=self.OWN, tmdb_id=12, notified=2)

        assert self._notify(emailed, "tmdb-movie-12") is True

    def test_a_show_recorded_by_its_tvdb_id_alone(self, emailed, library, arr):
        arr.series = [{"tmdbId": 77, "tvdbId": 7700, "seasons": []}]
        insert_download_notification(
            library, email=self.OWN, media_type="tv", service="sonarr", tmdb_id=None, tvdb_id=7700
        )

        assert self._notify(emailed, "tmdb-tv-77") is True

    def test_another_persons_is_not_told(self, emailed, library):
        insert_download_notification(library, email="other@example.com", tmdb_id=12)

        assert self._notify(emailed, "tmdb-movie-12") is False

    def test_one_already_sent_is_not_owed(self, emailed, library):
        insert_download_notification(library, email=self.OWN, tmdb_id=12, notified=1)

        assert self._notify(emailed, "tmdb-movie-12") is False

    def test_a_show_is_not_the_movie_of_the_same_tmdb_id(self, emailed, library):
        insert_download_notification(
            library, email=self.OWN, media_type="tv", service="sonarr", tmdb_id=12
        )

        assert self._notify(emailed, "tmdb-movie-12") is False

    def test_a_person_with_no_email_is_not_looked_up(self, viewer, tmdb, arr, monkeypatch):
        def looked_up(*_a, **_k):
            raise AssertionError("looked up")

        monkeypatch.setattr(catalog_api, "has_unsent_notification", looked_up)

        assert self._notify(viewer, "tmdb-movie-12") is False

    def test_a_refused_download_owes_nothing_and_an_accepted_one_does(self, emailed, library, arr):
        body = {"media_type": "movie", "tmdb_id": 12, "title": "Glass Onion"}
        arr.get_movie_by_tmdb = lambda _id: {"id": 1, "monitored": True}
        assert emailed.post("/api/search/download", json=body).status_code == 409
        assert self._notify(emailed, "tmdb-movie-12") is False

        arr.get_movie_by_tmdb = lambda _id: None
        assert emailed.post("/api/search/download", json=body).status_code == 200
        assert self._notify(emailed, "tmdb-movie-12") is True


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

    def test_a_search_409_shows_what_the_arr_holds_at_once(self, admin, tmdb, arr):
        search_download._download_dedup.clear()
        assert admin.get("/api/watch/title/tmdb-movie-12").json()["download"]["state"] is None
        arr.movies = [{"tmdbId": 12, "hasFile": False, "monitored": True}]
        arr.get_movie_by_tmdb = lambda _id: {"id": 1, "monitored": True}

        resp = admin.post(
            "/api/search/download",
            json={"media_type": "movie", "tmdb_id": 12, "title": "Glass Onion"},
        )
        assert resp.status_code == 409

        assert admin.get("/api/watch/title/tmdb-movie-12").json()["download"]["state"] == "queued"

    def test_a_search_409_for_a_show_shows_what_sonarr_holds_at_once(self, admin, tmdb, arr):
        search_download._download_dedup.clear()
        assert admin.get("/api/watch/title/tmdb-tv-77").json()["download"]["state"] is None
        arr.series = [{"tmdbId": 77, "tvdbId": 7700, "seasons": []}]
        arr.lookup_series_by_tmdb = lambda _id: {"tvdbId": 7700}

        resp = admin.post(
            "/api/search/download",
            json={"media_type": "tv", "tmdb_id": 77, "title": "Far Shore"},
        )
        assert resp.status_code == 409

        assert admin.get("/api/watch/title/tmdb-tv-77").json()["download"]["state"] == "queued"
