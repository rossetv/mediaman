"""The library's own genres and cast: the background fill, and what is read of it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.watch_catalog import _title_meta
from tests.helpers.factories import insert_media_item, insert_settings

NOON = datetime(2026, 6, 15, 12, tzinfo=UTC)


class Stub:
    def __init__(self) -> None:
        self.pages: dict[str, object] = {}
        self.asked: list[str] = []
        self.now = NOON
        self.seconds = 0.0

    def get(self, path, params=None):
        self.asked.append(path)
        answer = self.pages.get(path, 404)
        if isinstance(answer, int):
            raise SafeHTTPError(status_code=answer, body_snippet="", url="https://tmdb.invalid")
        return answer


@pytest.fixture
def stub(monkeypatch, tmp_path):
    stub = Stub()
    disk_cache.init(tmp_path)
    monkeypatch.setattr(TmdbClient, "_get", lambda _self, p, params=None: stub.get(p, params))
    monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: TmdbClient("token"))
    monkeypatch.setattr(disk_cache, "_failed", {})
    monkeypatch.setattr(_title_meta, "_failed", set())
    monkeypatch.setattr(_title_meta, "_resume_at", 0.0)
    monkeypatch.setattr(_title_meta, "_clock", lambda: stub.seconds)
    monkeypatch.setattr(_title_meta, "now_utc", lambda: stub.now)
    return stub


@pytest.fixture
def library(conn, stub):
    insert_settings(conn, plex_libraries='["1"]')

    def add(tmdb_id, *, show=False, tvdb_id=None, section=1, **page):
        key = str(tmdb_id or tvdb_id)
        insert_media_item(
            conn,
            id=key,
            plex_rating_key=key,
            title=f"Title {key}",
            media_type="tv_season" if show else "movie",
            show_rating_key=f"9{key}" if show else None,
            plex_library_id=section,
        )
        conn.execute(
            "UPDATE media_items SET tmdb_id = ?, tvdb_id = ? WHERE id = ?", (tmdb_id, tvdb_id, key)
        )
        conn.commit()
        if tmdb_id:
            stub.pages[f"/{'tv' if show else 'movie'}/{tmdb_id}"] = _page(tmdb_id, **page)

    return add


def _page(tmdb_id, *, genres=(28,), cast=(), **fields):
    return {
        "id": tmdb_id,
        "title": f"Title {tmdb_id}",
        "genres": [{"id": g, "name": f"Genre {g}"} for g in genres],
        "credits": {"cast": [c if isinstance(c, dict) else _actor(c) for c in cast]},
        **fields,
    }


def _actor(person_id, **fields):
    return {"id": person_id, "name": f"Actor {person_id}", "character": "Edith"} | fields


def _run(conn, stopped=lambda: False):
    catalog.fill_title_meta(conn, "key", stopped=stopped)


def _ids(conn, genre="action", **kwargs):
    items, _ = catalog.library_titles(conn, catalog.GENRES[genre], **kwargs)
    return [i["id"] for i in items]


def _coverage(conn):
    covered = catalog.library_people(conn).coverage
    return covered.known, covered.total, covered.ready


class TestFill:
    def test_a_run_asks_for_a_few_titles_and_the_next_goes_on(self, conn, stub, library):
        for n in range(1, 9):
            library(n)

        _run(conn)
        first = len(stub.asked)
        _run(conn)

        assert (first, sorted(stub.asked)) == (6, sorted(f"/movie/{n}" for n in range(1, 9)))
        _run(conn)
        assert len(stub.asked) == 8

    def test_a_title_already_cached_is_not_asked_for_and_costs_no_ask(self, conn, stub, library):
        for n in range(1, 9):
            library(n)
        for n in (1, 2):
            catalog.details(TmdbClient("token"), "movie", n)
        stub.asked.clear()

        _run(conn)

        assert "/movie/1" not in stub.asked
        assert _coverage(conn)[:2] == (8, 8)

    @pytest.mark.parametrize("status", [500, 429, 401])
    def test_a_failure_ends_the_run_and_leaves_tmdb_alone_a_while(
        self, conn, stub, library, status
    ):
        library(1)
        library(2)
        stub.pages = {"/movie/1": status, "/movie/2": status}

        _run(conn)
        stub.seconds = 14 * 60
        _run(conn)

        assert stub.asked == ["/movie/1"]

    def test_a_title_tmdb_cannot_answer_for_holds_up_no_other(self, conn, stub, library):
        library(1)
        library(2)
        stub.pages["/movie/1"] = 500

        _run(conn)
        stub.seconds = 16 * 60
        _run(conn)

        assert _ids(conn) == [2]

    def test_a_title_tmdb_has_not_or_marks_adult_is_settled_and_never_listed(
        self, conn, stub, library
    ):
        library(1, adult=True)
        library(2)
        stub.pages["/movie/2"] = 404

        _run(conn)
        _run(conn)

        assert (_ids(conn), len(stub.asked), _coverage(conn)) == ([], 2, (2, 2, True))

    def test_one_is_asked_for_again_after_a_month_and_an_absent_one_after_a_week(
        self, conn, stub, library, monkeypatch
    ):
        library(1)
        library(2)
        stub.pages["/movie/2"] = 404
        _run(conn)
        asked = []
        monkeypatch.setattr(
            _title_meta,
            "_fill_one",
            lambda _conn, _client, key, _allow: asked.append((stub.now - NOON, key[1])),
        )

        for days in (6, 8, 31):
            stub.now = NOON + timedelta(days=days)
            _run(conn)

        assert [(d.days, i) for d, i in asked] == [(8, 2), (31, 1), (31, 2)]

    def test_an_answer_older_than_a_week_is_asked_for_again(self, conn, stub, library, monkeypatch):
        library(1)
        monkeypatch.setattr(disk_cache.time, "time", lambda: NOON.timestamp())
        catalog.details(TmdbClient("token"), "movie", 1)
        monkeypatch.setattr(disk_cache.time, "time", lambda: NOON.timestamp() + 8 * 86400)
        stub.pages["/movie/1"] = _page(1, genres=(35,))

        _run(conn)

        assert _ids(conn, "comedy") == [1]

    def test_a_show_known_by_its_tvdb_id_is_found_through_it(self, conn, stub, library):
        library(None, show=True, tvdb_id=77)
        stub.pages["/find/77"] = {"tv_results": [{"id": 5}]}
        stub.pages["/tv/5"] = _page(5, genres=(10759,))

        _run(conn)

        [item], _ = catalog.library_titles(conn, catalog.GENRES["action"])
        assert (item["id"], item["media_type"], item["library_ref"]) == (5, "tv", "show-977")

    def test_a_deleted_title_is_gone_at_once_and_dropped_by_the_next_run(self, conn, stub, library):
        library(1, cast=[7])
        _run(conn)
        conn.execute("DELETE FROM media_items")

        assert _ids(conn) == []
        _run(conn)
        assert conn.execute("SELECT COUNT(*) FROM title_meta").fetchone()[0] == 0

    def test_a_library_not_selected_is_neither_asked_for_nor_listed(self, conn, stub, library):
        library(1)
        library(2)
        _run(conn)
        conn.execute("UPDATE media_items SET plex_library_id = 2 WHERE id = '1'")
        library(3, section=2)

        _run(conn)

        assert _ids(conn) == [2]
        assert "/movie/3" not in stub.asked
        # Its answer is kept: the library may be ticked again.
        assert conn.execute("SELECT COUNT(*) FROM title_meta").fetchone()[0] == 2

    def test_it_stops_between_titles_when_told(self, conn, stub, library):
        library(1)
        library(2)

        _run(conn, stopped=lambda: len(stub.asked) == 1)

        assert stub.asked == ["/movie/1"]


class TestLibraryTitles:
    def test_a_wide_tv_genre_is_in_both_it_covers(self, conn, stub, library):
        library(1, show=True, genres=(10759,))
        _run(conn)

        assert (_ids(conn, "action"), _ids(conn, "adventure")) == ([1], [1])
        assert _ids(conn, "comedy") == []

    def test_each_order_lists_every_title(self, conn, stub, library):
        library(1, popularity=5, vote_average=9.9, vote_count=2, release_date="2001-01-01")
        library(2, popularity=9, vote_average=7.0, vote_count=900, release_date="1999-01-01")
        library(3, popularity=7, vote_average=8.0, vote_count=900)
        library(
            4,
            show=True,
            genres=(10759,),
            popularity=900,
            vote_average=6,
            vote_count=250,
            first_air_date="2020-01-01",
        )
        _run(conn)

        assert _ids(conn, sort="popular") == [2, 4, 3, 1]
        assert _ids(conn, sort="top_rated") == [3, 2, 4, 1]
        assert _ids(conn, sort="newest") == [4, 1, 2, 3]

    def test_a_kind_and_a_decade_narrow_it(self, conn, stub, library):
        library(1, release_date="1994-05-01")
        library(2, release_date="2004-05-01")
        library(3, show=True, genres=(10759,), first_air_date="1999-12-31")
        _run(conn)

        assert sorted(_ids(conn, decade="1990s")) == [1, 3]
        assert _ids(conn, decade="1990s", media_type="tv") == [3]

    def test_two_copies_of_one_film_are_one_title(self, conn, stub, library):
        library(1)
        insert_media_item(conn, id="50", plex_rating_key="50", title="Copy")
        conn.execute("UPDATE media_items SET tmdb_id = 1 WHERE id = '50'")
        _run(conn)

        assert (_ids(conn), _coverage(conn)) == ([1], (1, 1, True))


class TestPeople:
    def test_people_are_counted_once_a_title_and_most_titles_come_first(self, conn, stub, library):
        library(1, cast=[7, 8, _actor(8), 9])
        library(2, cast=[8, _actor(7, profile_path="/seven.jpg")])
        library(3, cast=[8, 5])
        _run(conn)

        people = catalog.library_people(conn).people

        assert [(p["person_id"], p["titles"]) for p in people] == [(8, 3), (7, 2)]

    def test_adult_people_and_those_as_themselves_are_left_out(self, conn, stub, library):
        cast = [_actor(7, adult=True), _actor(8, character="Self - Host"), 9]
        library(1, cast=cast)
        library(2, cast=cast)
        _run(conn)

        people = catalog.library_people(conn).people

        assert [p["person_id"] for p in people] == [9]

    def test_actors_belong_to_the_genres_they_are_often_in(self, conn, stub, library):
        library(1, genres=(28,), cast=[7, 8])
        library(2, genres=(28, 35), cast=[7, 8])
        library(3, genres=(35,), cast=[8])
        _run(conn)

        actors = catalog.library_people(conn).actors

        assert {
            g: [(a["person_id"], a["titles"]) for a in found] for g, found in actors.items()
        } == {
            "action": [(7, 2), (8, 2)],
            "comedy": [(8, 2)],
        }

    def test_nobody_is_listed_before_the_library_is_filled(self, conn, stub, library):
        for n in range(1, 9):
            library(n, cast=[7])
        _run(conn)
        half = catalog.library_people(conn)
        _run(conn)

        assert (half.coverage.ready, half.people, half.actors) == (False, [], {})
        assert catalog.library_people(conn).people[0]["titles"] == 8

    def test_a_library_without_titles_is_not_ready(self, conn, stub):
        assert _coverage(conn) == (0, 0, False)
