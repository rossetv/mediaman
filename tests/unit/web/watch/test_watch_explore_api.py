"""Watch explore JSON: titles by genre and people, for viewers and admins alike."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mediaman.core.time import now_utc
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.watch_catalog import _arr, _library
from mediaman.web.auth.session_store import create_session
from mediaman.web.repository.watch_progress import MarkTarget, mark_watched
from mediaman.web.routes.watch import catalog_api, explore_api
from tests.helpers.factories import insert_admin_user, insert_media_item, insert_settings

EXPLORE = "/api/watch/explore"
PERSON = "/api/watch/person/7"
PEOPLE_A_DAY = explore_api._PERSON_LIMITER._max_per_day


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    catalog.invalidate()
    disk_cache.init(tmp_path)
    monkeypatch.setattr(disk_cache, "sync", True)
    monkeypatch.setattr(_library, "_tvdb", {})
    monkeypatch.setattr(_library, "_held", {})
    monkeypatch.setattr(_library, "sync", True)
    queue = [{"tmdbId": 13, "hasFile": False, "monitored": True}]
    radarr = SimpleNamespace(get_movies=lambda: queue, get_queue=list)
    monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", lambda *_: radarr)
    monkeypatch.setattr(_arr.arr_build, "build_sonarr_from_db", lambda *_: None)
    for name in ("_EXPLORE_LIMITER", "_PERSON_LIMITER"):
        monkeypatch.setattr(explore_api, name, ActionRateLimiter(60, 60))
    yield
    catalog.invalidate()


@pytest.fixture
def tmdb(monkeypatch):
    """TMDB's answers by path; a status code fails."""
    answers: dict[str, object] = {}

    def get(self, path, params=None):
        answer = answers[path]
        if isinstance(answer, int):
            raise SafeHTTPError(status_code=answer, body_snippet="", url="https://tmdb.invalid")
        return answer

    monkeypatch.setattr(TmdbClient, "_get", get)
    monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: TmdbClient("token"))
    return answers


@pytest.fixture
def no_tmdb(monkeypatch):
    monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: None)


@pytest.fixture
def app(app_factory, conn):
    insert_settings(conn, plex_libraries='["1"]')
    insert_media_item(conn, id="100", plex_rating_key="100", title="The Glasshouse")
    conn.execute("UPDATE media_items SET tmdb_id = 11 WHERE id = '100'")
    conn.commit()
    return app_factory(explore_api.router, catalog_api.router, conn=conn)


@pytest.fixture
def viewer(app, conn):
    insert_admin_user(conn, username="viewer", role="viewer")
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "viewer"))
    return client


def _movies(*ids, pages=1):
    items = [{"id": i, "title": f"Film {i}", "media_type": "movie"} for i in ids]
    return {"results": items, "total_pages": pages, "total_results": len(ids)}


class TestExplore:
    def test_a_card_is_the_one_discover_gives_the_same_title(self, viewer, conn, tmdb):
        mark_watched(conn, "viewer", [MarkTarget("100", "100", None, 0)], now_utc())
        tmdb["/discover/movie"] = tmdb["/trending/all/week"] = _movies(11, 12, 13)
        tmdb["/movie/popular"] = tmdb["/tv/popular"] = 500

        body = viewer.get(EXPLORE, params={"genre": "horror"}).json()
        discover = viewer.get("/api/watch/discover").json()["rows"][0]["items"]

        assert body["items"] == discover
        assert [(i["library_ref"], i["watched"], i["download_state"]) for i in body["items"]] == [
            ("movie-100", True, None),
            (None, False, None),
            (None, False, "queued"),
        ]

    def test_it_says_what_is_shown_and_how_to_go_on(self, viewer, tmdb):
        tmdb["/discover/movie"] = _movies(12, pages=2)

        body = viewer.get(
            EXPLORE,
            params={"genre": "horror", "type": "movie", "sort": "top_rated", "decade": "1990s"},
        ).json()

        assert body["genre"] == {"id": "horror", "name": "Horror", "types": ["movie"]}
        assert [body[k] for k in ("type", "sort", "decade", "total", "next_page")] == [
            "movie",
            "top_rated",
            "1990s",
            1,
            2,
        ]

    @pytest.mark.parametrize(
        ("params", "status"),
        [
            ({"genre": "nope"}, 404),
            ({"genre": "27"}, 404),
            ({"genre": "horror", "type": "person"}, 400),
            ({"genre": "horror", "sort": "revenue.desc"}, 400),
            ({"genre": "horror", "decade": "1990"}, 400),
            ({"genre": "horror", "decade": "1920s"}, 400),
            ({"genre": "horror", "page": 0}, 422),
            ({"genre": "horror", "page": 501}, 422),
        ],
    )
    def test_anything_off_the_lists_is_refused_before_tmdb_is_asked(
        self, viewer, tmdb, params, status
    ):
        # An ask of TMDB has no answer here and would raise.
        assert viewer.get(EXPLORE, params=params).status_code == status

    @pytest.mark.parametrize(("failure", "status"), [(500, 502), (429, 429)])
    def test_tmdb_failing_is_an_error_and_not_an_empty_list(self, viewer, tmdb, failure, status):
        tmdb["/discover/movie"] = failure

        assert viewer.get(EXPLORE, params={"genre": "horror"}).status_code == status

    def test_past_the_limit_only_a_page_tmdb_must_answer_is_refused(
        self, viewer, tmdb, monkeypatch
    ):
        monkeypatch.setattr(explore_api, "_EXPLORE_LIMITER", ActionRateLimiter(1, 60))
        tmdb["/discover/movie"] = _movies(12, pages=9)

        codes = [
            viewer.get(EXPLORE, params={"genre": "horror", "page": n}).status_code
            for n in (1, 1, 2)
        ]

        assert codes == [200, 200, 429]

    def test_without_tmdb_it_is_empty_and_no_error(self, viewer, no_tmdb):
        body = viewer.get(EXPLORE, params={"genre": "horror"}).json()

        assert (body["tmdb_configured"], body["items"], body["next_page"]) == (False, [], None)


class TestGenres:
    def test_the_categories_come_with_the_posters_already_held(self, viewer, tmdb, monkeypatch):
        monkeypatch.setattr(explore_api, "_EXPLORE_LIMITER", ActionRateLimiter(1, 60))
        tmdb["/discover/tv"] = 500
        tmdb["/discover/movie"] = {"results": [{"id": 1, "poster_path": "/p.jpg"}]}
        viewer.get(EXPLORE)

        body = viewer.get(EXPLORE).json()

        first = body["genres"][0]
        assert (first["id"], first["types"]) == ("action", ["movie", "tv"])
        assert first["poster_urls"] == ["/api/watch/img/tmdb/w185/p.jpg"]
        assert body["decades"][-1] == "1930s"

    def test_without_tmdb_they_are_still_listed(self, viewer, no_tmdb):
        body = viewer.get(EXPLORE).json()

        assert len(body["genres"]) == len(catalog.GENRES)
        assert not body["tmdb_configured"]


def _person(*cast, **fields):
    return {
        "name": "Mara Ellison",
        "known_for_department": "Acting",
        "combined_credits": {"cast": list(cast)},
    } | fields


def _credit(tmdb_id, votes, **fields):
    return {
        "id": tmdb_id,
        "media_type": "movie",
        "title": f"Film {tmdb_id}",
        "vote_count": votes,
        "character": "Edith",
    } | fields


class TestPerson:
    def test_what_the_library_holds_comes_first_and_the_best_known_leads(self, viewer, tmdb):
        tmdb["/person/7"] = _person(
            _credit(21, 900),
            _credit(13, 800),
            _credit(11, 5),
            _credit(22, 9999, character="Self"),
            birthday="1920-01-01",
            deathday="1990-06-01",
        )

        body = viewer.get(PERSON).json()

        assert [c["ref"] for c in body["items"]] == [f"tmdb-movie-{n}" for n in (11, 21, 13, 22)]
        assert [c["ref"] for c in body["known_for"]] == [f"tmdb-movie-{n}" for n in (21, 13, 11)]
        assert (body["total"], body["in_library"], body["next_page"]) == (4, 1, None)
        held, _, queued, _ = body["items"]
        assert (held["library_ref"], held["mark_key"], held["role"]) == (
            "movie-100",
            "100",
            "Edith",
        )
        assert queued["download_state"] == "queued"
        person = body["person"]
        assert (person["person_id"], person["job"], person["age"]) == (7, "Actor", 70)

    def test_a_long_career_comes_in_pages_without_a_gap_or_repeat(self, viewer, tmdb):
        tmdb["/person/7"] = _person(*(_credit(n, n) for n in range(100, 230)))

        pages = [viewer.get(PERSON, params={"page": n}).json() for n in (1, 2, 3, 4)]

        refs = [c["ref"] for p in pages for c in p["items"]]
        assert len(refs) == len(set(refs)) == 130
        assert [p["next_page"] for p in pages] == [2, 3, None, None]

    def test_a_title_the_library_gains_or_loses_between_pages_is_still_listed(
        self, viewer, conn, tmdb
    ):
        # Best known first: 230 down to 101, with the held title 11 last.
        tmdb["/person/7"] = _person(*(_credit(n, n) for n in range(101, 231)), _credit(11, 1))

        def refs(page):
            body = viewer.get(PERSON, params={"page": page}).json()
            return [c["ref"] for c in body["items"]]

        def hold(tmdb_id):
            conn.execute("UPDATE media_items SET tmdb_id = ? WHERE id = '100'", (tmdb_id,))
            conn.commit()
            catalog.clear_library_index()

        first = refs(1)
        hold(105)
        second = refs(2)
        hold(999)
        third = refs(3)

        assert first[0] == "tmdb-movie-11"
        assert first[1:3] == ["tmdb-movie-230", "tmdb-movie-229"]
        assert len(first) == 61
        assert len(set(first + second + third)) == 131

    def test_someone_only_seen_in_small_parts_is_known_for_those(self, viewer, tmdb):
        tmdb["/person/7"] = _person(_credit(21, 9, character="Self"))

        assert [c["ref"] for c in viewer.get(PERSON).json()["known_for"]] == ["tmdb-movie-21"]

    @pytest.mark.parametrize("person_id", ["0", "x", "-1", "1.5", "12345678901", "٣"])
    def test_a_malformed_id_is_not_found(self, viewer, tmdb, person_id):
        assert viewer.get(f"/api/watch/person/{person_id}").status_code == 404

    @pytest.mark.parametrize(
        ("answer", "status"), [(404, 404), (500, 502), (429, 429), (_person(adult=True), 404)]
    )
    def test_no_such_person_and_a_failure_are_told_apart(self, viewer, tmdb, answer, status):
        tmdb["/person/7"] = answer

        assert viewer.get(PERSON).status_code == status

    def test_past_the_limit_only_a_person_tmdb_must_answer_for_is_refused(
        self, viewer, tmdb, monkeypatch
    ):
        monkeypatch.setattr(explore_api, "_PERSON_LIMITER", ActionRateLimiter(1, 60))
        tmdb["/person/7"] = tmdb["/person/8"] = _person()

        codes = [viewer.get(f"/api/watch/person/{n}").status_code for n in (7, 7, 8)]

        assert codes == [200, 200, 429]

    def test_the_people_one_person_may_look_up_in_a_day_leave_the_cache_to_the_rest(
        self, viewer, tmdb, tmp_path
    ):
        tmdb["/person/7"] = _person(
            *(
                _credit(
                    10**6 + n,
                    n,
                    title="A fairly long title " * 2,
                    release_date="2019-03-01",
                    poster_path=f"/{'p' * 27}.jpg",
                    vote_average=7.123,
                    character="A fairly long part",
                )
                for n in range(1000)
            )
        )
        viewer.get(PERSON)

        [largest] = [p.stat().st_size for p in (tmp_path / "watch_cache" / "meta").iterdir()]

        assert largest * PEOPLE_A_DAY < disk_cache.meta().max_bytes / 2

    def test_without_tmdb_there_is_no_person_and_no_error(self, viewer, no_tmdb):
        body = viewer.get(PERSON).json()

        assert (body["ok"], body["tmdb_configured"], body["person"]) == (True, False, None)


@pytest.mark.parametrize("path", [EXPLORE, PERSON])
def test_signed_out_is_refused(app, no_tmdb, path):
    assert TestClient(app).get(path).status_code == 401
