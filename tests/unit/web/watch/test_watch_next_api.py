"""Watch next JSON: an endless mix of what the person may like, one cursor at a time."""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.core.time import now_utc
from mediaman.crypto import generate_watch_next_cursor
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai import client as openai_client
from mediaman.services.watch_catalog import _arr, _library
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.watch import next_api
from mediaman.web.routes.watch.next_mix import BLOOM_BYTES, MAX_SEEDS, Bloom, MixState
from tests.helpers.factories import (
    insert_admin_user,
    insert_media_item,
    insert_settings,
    insert_suggestion,
)

NEXT = "/api/watch/next"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    catalog.invalidate()
    disk_cache.init(tmp_path)
    monkeypatch.setattr(disk_cache, "sync", True)
    monkeypatch.setattr(_library, "_tvdb", {})
    monkeypatch.setattr(_library, "_held", {})
    monkeypatch.setattr(_library, "sync", True)
    monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", lambda *_: None)
    monkeypatch.setattr(_arr.arr_build, "build_sonarr_from_db", lambda *_: None)
    monkeypatch.setattr(next_api, "_held", {})
    yield
    catalog.invalidate()


class _NoOpenAI:
    def post(self, *_a, **_k):
        raise AssertionError("Watch next asked the AI")


@pytest.fixture(autouse=True)
def _no_openai(monkeypatch):
    monkeypatch.setattr(openai_client, "_OPENAI_CLIENT", _NoOpenAI())


def _titles(base: int, kind: str = "movie", count: int = 20) -> list[dict[str, object]]:
    return [
        {"id": base + i, "title": f"T{base + i}", "media_type": kind, "poster_path": "/p.jpg"}
        for i in range(count)
    ]


@pytest.fixture
def tmdb(monkeypatch):
    """TMDB's answers by path: a base id, its pages a thousand ids apart; a list of results; an
    answer made of the asked page's params; or a status code, which fails."""
    answers: dict[str, object] = {
        "/trending/all/week": 1000,
        "/movie/popular": 500_000,
        "/tv/popular": 600_000,
    }

    def get(self, path, params=None):
        answer = answers[path]
        if callable(answer):
            return answer(params or {})
        if isinstance(answer, int) and answer < 1000:
            raise SafeHTTPError(status_code=answer, body_snippet="", url="https://tmdb.invalid")
        if isinstance(answer, int):
            page = (params or {}).get("page", 1)
            kind = "tv" if path.startswith("/tv") else "movie"
            return {"results": _titles(answer + page * 1000, kind), "total_pages": 500}
        return {"results": answer, "total_pages": 1}

    monkeypatch.setattr(TmdbClient, "_get", get)
    monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: TmdbClient("token"))
    return answers


@pytest.fixture
def app(app_factory, conn):
    insert_settings(conn, plex_libraries='["1"]')
    return app_factory(next_api.router, conn=conn)


@pytest.fixture
def viewer(app, conn):
    insert_admin_user(conn, username="viewer", role="viewer")
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "viewer"))
    return client


def _user_id(conn) -> int:
    return conn.execute("SELECT id FROM admin_users WHERE username = 'viewer'").fetchone()[0]


def _played(conn, *tmdb_ids: int) -> None:
    for n, tmdb_id in enumerate(tmdb_ids):
        at = (now_utc() - timedelta(hours=n)).isoformat()
        conn.execute(
            "INSERT INTO played_titles (username, media_type, title, tmdb_id, played_at)"
            " VALUES ('viewer', 'movie', ?, ?, ?)",
            (f"Played {tmdb_id}", tmdb_id, at),
        )
    conn.commit()


def _searched(conn, phrase: str) -> None:
    conn.execute(
        "INSERT INTO user_searches (user_id, phrase_key, phrase, searched_at) VALUES (?, ?, ?, ?)",
        (_user_id(conn), phrase.casefold(), phrase, now_utc().isoformat()),
    )
    conn.commit()


def _ids(body) -> list[str]:
    return [i["tmdb_ref"] or i["ref"] for i in body["items"]]


def _reasons(body) -> list[str]:
    return [i["reason"]["kind"] for i in body["items"]]


class TestNewPerson:
    def test_someone_with_no_history_gets_trending_and_popular_in_turn(self, viewer, tmdb):
        body = viewer.get(NEXT).json()

        assert body["personal"] is False
        assert len(body["items"]) == 24
        assert _reasons(body)[:4] == ["trending", "popular", "trending", "popular"]

    def test_the_trending_row_s_own_first_page_is_left_out(self, viewer, tmdb):
        body = viewer.get(NEXT).json()

        trending = [i["tmdb_ref"] for i in body["items"] if i["reason"]["kind"] == "trending"]
        assert trending[0] == "tmdb-movie-3000"  # the list's second page; its first is a row


class TestCursor:
    def test_the_next_page_gives_no_title_the_first_gave_though_a_list_gives_it_again(
        self, viewer, tmdb
    ):
        def again(params):
            first_page = params.get("page", 1) == 1
            return {"results": _titles(2000 if first_page else 3000), "total_pages": 9}

        tmdb["/trending/all/week"] = again
        first = viewer.get(NEXT).json()
        second = viewer.get(NEXT, params={"cursor": first["cursor"]}).json()

        assert len(second["items"]) == 24
        assert "trending" in _reasons(second)
        assert not set(_ids(first)) & set(_ids(second))

    @pytest.mark.parametrize(
        "spoil",
        [
            lambda c: c[:-2] + ("AA" if not c.endswith("AA") else "BB"),
            lambda c: "garbage",
            lambda c: c * 4,
            lambda c: "",
        ],
    )
    def test_a_cursor_not_as_issued_starts_again_from_the_head(self, viewer, tmdb, spoil):
        first = viewer.get(NEXT).json()

        again = viewer.get(NEXT, params={"cursor": spoil(first["cursor"])})

        assert again.status_code == 200
        assert _ids(again.json()) == _ids(first)

    def test_a_cursor_of_another_filter_starts_that_filter_from_its_head(self, viewer, tmdb):
        films = viewer.get(NEXT, params={"type": "movie"}).json()

        shows = viewer.get(NEXT, params={"type": "tv", "cursor": films["cursor"]}).json()

        assert shows["items"] and {i["kind"] for i in shows["items"]} == {"show"}
        assert _ids(shows) == _ids(viewer.get(NEXT, params={"type": "tv"}).json())

    def test_a_signed_cursor_of_another_person_starts_again(self, viewer, tmdb, secret_key):
        state = {"v": 1, "u": "someone-else", "t": "all", "m": MixState().as_json()}
        cursor = generate_watch_next_cursor(
            state={**state, "b": Bloom().text()}, ttl_s=60, secret_key=secret_key
        )

        body = viewer.get(NEXT, params={"cursor": cursor}).json()

        assert _ids(body) == _ids(viewer.get(NEXT).json())

    def test_the_fullest_cursor_still_fits_the_signed_token(self, secret_key):
        places = {k: (MAX_SEEDS - 1, [[501, 1000]] * MAX_SEEDS) for k in next_api.WEIGHTS}
        state = MixState(dict.fromkeys(next_api.WEIGHTS, -100), "popular", places)
        bloom = Bloom(bytes([0xFF]) * BLOOM_BYTES)
        cursor = generate_watch_next_cursor(
            state={"v": 1, "u": "f" * 16, "t": "movie", "m": state.as_json(), "b": bloom.text()},
            ttl_s=60,
            secret_key=secret_key,
        )

        read, _ = next_api._read_cursor(cursor, secret_key, "f" * 16, "movie")

        assert read.last == "popular"

    def test_an_unknown_filter_is_refused(self, viewer, tmdb):
        assert viewer.get(NEXT, params={"type": "anime"}).status_code == 400


class TestFilter:
    def test_tv_gives_shows_alone_and_movie_films_alone(self, viewer, tmdb):
        tv = viewer.get(NEXT, params={"type": "tv"}).json()["items"]
        films = viewer.get(NEXT, params={"type": "movie"}).json()["items"]

        assert {i["kind"] for i in tv} == {"show"}
        assert {i["kind"] for i in films} == {"movie"}


class TestPersonal:
    def test_the_mix_takes_in_every_source_with_something_to_give(self, viewer, conn, tmdb):
        _played(conn, 77)
        _searched(conn, "quiet anime")
        tmdb["/movie/77/recommendations"] = _titles(70_000)
        tmdb["/search/multi"] = _titles(80_000)
        insert_suggestion(conn, title="Shown", tmdb_id=90_001, batch_id="2099-01-02")
        for n in range(10):
            insert_suggestion(
                conn, title=f"Older {n}", tmdb_id=91_000 + n, batch_id="2099-01-01", reason="Yes."
            )

        body = viewer.get(NEXT).json()

        kinds = _reasons(body)
        assert body["personal"] is True
        assert {"ai", "watched", "search", "trending"} <= set(kinds)
        assert all(a != b for a, b in itertools.pairwise(kinds))
        ai = [i for i in body["items"] if i["reason"]["kind"] == "ai"]
        assert ai[0]["reason"]["why"] == "Yes."
        assert "tmdb-movie-90001" not in _ids(body)  # the Recommended row shows it
        watched = next(i for i in body["items"] if i["reason"]["kind"] == "watched")
        assert watched["reason"]["text"] == "Because you watched Played 77"

    def test_searches_are_left_out_when_the_person_keeps_no_history(self, viewer, conn, tmdb):
        _searched(conn, "quiet anime")
        tmdb["/search/multi"] = _titles(80_000)
        conn.execute("UPDATE admin_users SET ai_history = 0 WHERE username = 'viewer'")
        conn.commit()

        body = viewer.get(NEXT).json()

        assert "search" not in _reasons(body)
        assert body["personal"] is False

    def test_a_search_that_named_a_library_title_is_not_taken_for_a_taste(self, viewer, conn, tmdb):
        insert_media_item(conn, id="100", plex_rating_key="100", title="Paper Cranes")
        _searched(conn, "paper cranes")
        tmdb["/search/multi"] = _titles(80_000)

        body = viewer.get(NEXT).json()

        assert body["personal"] is True
        assert "search" not in _reasons(body)

    def test_a_failing_source_is_skipped_and_the_page_still_fills(self, viewer, conn, tmdb):
        _played(conn, 77)
        tmdb["/movie/77/recommendations"] = 500

        response = viewer.get(NEXT)

        assert response.status_code == 200
        assert len(response.json()["items"]) == 24
        assert "watched" not in _reasons(response.json())


class TestLeftOut:
    def test_a_title_on_the_first_page_of_a_library_row_is_left_out(self, viewer, conn, tmdb):
        insert_media_item(conn, id="100", plex_rating_key="100", title="T3000")
        conn.execute("UPDATE media_items SET tmdb_id = 3000 WHERE id = '100'")
        conn.commit()

        assert "tmdb-movie-3000" not in _ids(viewer.get(NEXT).json())

    def test_titles_marked_watched_or_rated_down_are_left_out(self, viewer, conn, tmdb):
        user = _user_id(conn)
        conn.execute(
            "INSERT INTO user_ratings (user_id, media_type, tmdb_id, level, updated_at)"
            " VALUES (?, 'movie', 3000, -1, ?)",
            (user, now_utc().isoformat()),
        )
        conn.execute(
            "INSERT INTO user_seen_titles (user_id, media_type, tmdb_id, seen_at)"
            " VALUES (?, 'movie', 3001, ?)",
            (user, now_utc().isoformat()),
        )
        conn.commit()

        ids = _ids(viewer.get(NEXT).json())

        assert "tmdb-movie-3000" not in ids and "tmdb-movie-3001" not in ids
        assert "tmdb-movie-3002" in ids
