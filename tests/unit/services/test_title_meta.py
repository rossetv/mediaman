"""The library's own genres and cast: the background fill, and what is read of it."""

from __future__ import annotations

import sqlite3
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
    monkeypatch.setattr(_title_meta, "_failed", {})
    monkeypatch.setattr(_title_meta, "_unreadable_token", None)
    monkeypatch.setattr(_title_meta, "_down", None)
    monkeypatch.setattr(_title_meta, "_library", {})
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

    @pytest.mark.parametrize("path", ["movie", "find"])
    @pytest.mark.parametrize(("status", "asks"), [(500, 2), (429, 1), (401, 1), (403, 1)])
    def test_an_outage_ends_the_run_and_leaves_tmdb_alone_a_while(
        self, conn, stub, library, status, asks, path
    ):
        for n in (1, 2, 3):
            if path == "movie":
                library(n)
            else:
                library(None, show=True, tvdb_id=n)
            stub.pages[f"/{path}/{n}"] = status

        _run(conn)
        stub.seconds = 14 * 60
        _run(conn)

        assert stub.asked == [f"/{path}/{n}" for n in range(1, asks + 1)]

    def test_tmdb_failing_on_one_title_alone_holds_up_no_other(self, conn, stub, library):
        for n in (1, 2, 3):
            library(n)
        stub.pages["/movie/1"] = 500

        _run(conn)

        assert sorted(_ids(conn)) == [2, 3]

    @pytest.mark.parametrize("between", [200, 404, 400])
    def test_two_failures_with_an_answer_between_are_no_outage(self, conn, stub, library, between):
        for n in (1, 2, 3, 4):
            library(n)
        stub.pages["/movie/1"] = stub.pages["/movie/3"] = 500
        if between != 200:
            stub.pages["/movie/2"] = between

        _run(conn)

        assert 4 in _ids(conn)

    def test_an_outage_goes_on_at_the_next_failure_and_takes_back_no_other_ask(
        self, conn, stub, library
    ):
        library(9)
        for quarter in (0, 1):
            stub.pages["/movie/9"] = 400
            stub.seconds = quarter * 1000
            _run(conn)
        for quarter, n in ((2, 1), (3, 2)):
            library(n)
            stub.pages[f"/movie/{n}"] = stub.pages["/movie/9"] = 500
            stub.seconds = quarter * 1000
            stub.asked.clear()
            _run(conn)
        assert stub.asked == ["/movie/2"]

        stub.pages["/movie/1"], stub.pages["/movie/2"] = _page(1), _page(2)
        stub.pages["/movie/9"] = 400
        stub.seconds = 4000
        _run(conn)

        assert _coverage(conn)[:2] == (3, 3)

    def test_a_title_alone_tmdb_fails_on_is_settled_after_three_asks(self, conn, stub, library):
        library(1)
        library(2)
        stub.pages["/movie/1"] = 500

        for quarter in range(5):
            stub.seconds = quarter * 1000
            _run(conn)

        assert (stub.asked.count("/movie/1"), _coverage(conn)) == (3, (2, 2, True))

    def test_an_outage_settles_no_title(self, conn, stub, library):
        for n in range(1, 4):
            library(n)
            stub.pages[f"/movie/{n}"] = 500

        for quarter in range(12):
            stub.seconds = quarter * 1000
            _run(conn)

        assert _coverage(conn)[0] == 0
        assert len(stub.asked) <= 2 * 12

    def test_asks_met_by_an_outage_neither_count_nor_take_back_those_before(
        self, conn, stub, library
    ):
        library(1)
        for quarter, status in enumerate((400, 400, 429, 400, 400)):
            stub.pages["/movie/1"] = status
            stub.seconds = quarter * 1000
            _run(conn)
            if status == 429:
                assert _title_meta._failed == {("movie", 1): (2, stub.seconds + 900)}

        assert (len(stub.asked), _coverage(conn)[0]) == (4, 1)

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
        catalog.clear_library_index()

        assert _ids(conn) == []
        _run(conn)
        assert conn.execute("SELECT COUNT(*) FROM title_meta").fetchone()[0] == 0

    def test_a_library_not_selected_is_neither_asked_for_nor_listed(self, conn, stub, library):
        library(1)
        library(2)
        _run(conn)
        conn.execute("UPDATE media_items SET plex_library_id = 2 WHERE id = '1'")
        library(3, section=2)
        catalog.clear_library_index()

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

    @pytest.mark.parametrize("bad", [400, "gone-genre", "huge-find"])
    def test_a_title_that_cannot_be_read_neither_pauses_nor_ends_the_run(
        self, conn, stub, library, monkeypatch, bad
    ):
        library(1, genres=(28,))
        library(2, genres=(35,))
        if bad == 400:
            stub.pages["/movie/1"] = 400
        elif bad == "gone-genre":
            monkeypatch.setattr(
                _title_meta, "GENRES", {k: g for k, g in catalog.GENRES.items() if k != "action"}
            )
        else:
            library(None, show=True, tvdb_id=77)
            stub.pages["/find/77"] = {"tv_results": [{"id": 2**70}]}

        _run(conn)

        assert _ids(conn, "comedy") == [2]
        # What can be read of the answer settles the title; a failure does not.
        assert _coverage(conn)[:2] == {400: (1, 2), "gone-genre": (2, 2), "huge-find": (3, 3)}[bad]

    def test_a_title_that_failed_is_left_alone_a_while(self, conn, stub, library):
        library(1)
        stub.pages["/movie/1"] = 400

        for minute in (0, 1, 14):
            stub.seconds = minute * 60
            _run(conn)

        assert stub.asked == ["/movie/1"]

    def test_a_title_failing_in_a_way_nobody_thought_of_ends_no_run(
        self, conn, stub, library, monkeypatch
    ):
        library(1)
        library(2)
        real = _title_meta._kept

        def kept(fields, kind):
            if fields["title"] == "Title 1":
                raise OverflowError
            return real(fields, kind)

        monkeypatch.setattr(_title_meta, "_kept", kept)

        _run(conn)

        assert _ids(conn) == [2]

    def test_a_failure_of_the_database_is_not_taken_for_the_titles(
        self, conn, stub, library, monkeypatch
    ):
        library(1)

        def locked(*_args):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(_title_meta, "_kept", locked)

        with pytest.raises(sqlite3.OperationalError):
            _run(conn)

    def test_failures_held_for_someone_else_pause_nothing(self, conn, stub, library):
        for n in (1, 2, 3):
            library(n)
        stub.pages["/movie/1"] = stub.pages["/movie/2"] = 500
        for n in (1, 2):
            with pytest.raises(catalog.TmdbUnavailable):
                catalog.details(TmdbClient("token"), "movie", n)

        _run(conn)

        assert _ids(conn) == [3]

    def test_a_title_that_keeps_failing_counts_as_settled_after_three_asks(
        self, conn, stub, library
    ):
        for n in range(1, 7):
            library(n)
            if n < 6:
                stub.pages[f"/movie/{n}"] = 400

        for minute in (0, 1, 16, 32, 48):
            stub.seconds = minute * 60
            _run(conn)

        assert [stub.asked.count(f"/movie/{n}") for n in (1, 5, 6)] == [3, 3, 1]
        assert _coverage(conn) == (6, 6, True)

    def test_what_is_held_of_a_title_stays_when_its_refresh_keeps_failing(
        self, conn, stub, library, monkeypatch
    ):
        library(1)
        _run(conn)
        stub.now += timedelta(days=31)
        later = disk_cache.time.time() + 31 * 86400
        monkeypatch.setattr(disk_cache.time, "time", lambda: later)
        stub.pages["/movie/1"] = 400

        for quarter in range(5):
            stub.seconds = quarter * 1000
            _run(conn)

        assert (_ids(conn), stub.asked.count("/movie/1")) == ([1], 4)

    @pytest.mark.parametrize("titles", [1, 3, 8])
    def test_a_run_in_which_every_title_failed_leaves_tmdb_alone_a_while(
        self, conn, stub, library, titles
    ):
        for n in range(1, titles + 1):
            library(n)
            stub.pages[f"/movie/{n}"] = 400
        _run(conn)
        asked = len(stub.asked)
        library(9)

        stub.seconds = 14 * 60
        _run(conn)

        assert (asked, len(stub.asked)) == (min(titles, 6),) * 2

    def test_a_run_with_one_answer_among_failures_is_no_outage(self, conn, stub, library):
        for n in (1, 2, 3):
            library(n)
        stub.pages["/movie/1"] = stub.pages["/movie/2"] = 400
        _run(conn)
        library(4)

        _run(conn)

        assert sorted(_ids(conn)) == [3, 4]

    def test_a_cached_title_does_not_hide_that_every_ask_failed(self, conn, stub, library):
        library(1)
        library(2)
        catalog.details(TmdbClient("token"), "movie", 1)
        stub.pages["/movie/2"] = 400
        _run(conn)
        library(3)

        _run(conn)

        assert "/movie/3" not in stub.asked

    @pytest.mark.parametrize("held", [False, True])
    def test_a_title_settled_by_failing_is_tried_again_after_a_day(
        self, conn, stub, library, monkeypatch, held
    ):
        library(1)
        if held:
            _run(conn)
            stub.now += timedelta(days=31)
            later = disk_cache.time.time() + 31 * 86400
            monkeypatch.setattr(disk_cache.time, "time", lambda: later)
        stub.pages["/movie/1"] = 400
        for quarter in range(4):
            stub.seconds = quarter * 1000
            _run(conn)
        settled = len(stub.asked)

        stub.now += timedelta(hours=23)
        _run(conn)
        before = len(stub.asked)
        stub.now += timedelta(hours=2)
        _run(conn)

        assert (settled, before, len(stub.asked)) == (3 + held, 3 + held, 4 + held)

    def test_asking_for_a_tvdb_id_counts_as_an_ask(self, conn, stub, library):
        for n in range(1, 9):
            library(None, show=True, tvdb_id=n)
            stub.pages[f"/find/{n}"] = {"tv_results": []}

        _run(conn)

        assert len(stub.asked) == 6

    def test_a_show_with_both_ids_is_asked_for_by_its_tmdb_id(self, conn, stub, library):
        library(5, show=True, tvdb_id=77)

        _run(conn)

        assert stub.asked == ["/tv/5"]

    def test_a_failed_ask_is_not_held_against_who_opens_the_title(self, conn, stub, library):
        library(1)
        stub.pages["/movie/1"] = 500
        _run(conn)
        stub.pages["/movie/1"] = _page(1)

        assert catalog.details(TmdbClient("token"), "movie", 1)["title"] == "Title 1"

    def test_without_tmdb_it_only_drops_what_is_gone(self, conn, stub, library, monkeypatch):
        library(1)
        _run(conn)
        conn.execute("DELETE FROM media_items")
        library(2)
        monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: None)

        for quarter in range(4):
            stub.seconds = quarter * 1000
            _run(conn)

        assert conn.execute("SELECT COUNT(*) FROM title_meta").fetchone()[0] == 0

    def test_a_token_that_cannot_be_read_is_tried_once_an_hour_or_when_it_changes(
        self, conn, stub, library, monkeypatch
    ):
        tried = []
        monkeypatch.setattr(TmdbClient, "from_db", lambda *_a, **_k: tried.append(1))
        insert_settings(conn, tmdb_read_token="garbled", encrypted=1)

        _run(conn)
        _run(conn)
        first = len(tried)
        conn.execute("UPDATE settings SET value = 'other' WHERE key = 'tmdb_read_token'")
        _run(conn)

        changed = len(tried)
        stub.seconds = 59 * 60
        _run(conn)
        within = len(tried)
        stub.seconds = 61 * 60
        _run(conn)

        assert (first, changed, within, len(tried)) == (1, 2, 2, 3)

    def test_details_cached_without_what_the_fill_reads_are_not_used(
        self, conn, stub, library, tmp_path
    ):
        library(1)
        disk_cache.meta().get_or_fetch(
            "tmdb:details:v4:movie:1", 1e9, 1e9, lambda: {"title": "Old"}
        )

        _run(conn)

        assert _ids(conn) == [1]


class TestHeld:
    def test_the_table_is_read_once_a_minute(self, conn, stub, library, monkeypatch):
        library(1)
        _run(conn)
        built = []
        real = _title_meta._build
        monkeypatch.setattr(_title_meta, "_build", lambda c: built.append(1) or real(c))

        for seconds in (0, 59, 61):
            stub.seconds = seconds
            _ids(conn)
            catalog.library_people(conn)

        assert len(built) == 2

    def test_a_run_that_fills_a_title_shows_it_at_once(self, conn, stub, library):
        library(1)
        _run(conn)
        _ids(conn)
        library(2)

        _run(conn)

        assert sorted(_ids(conn)) == [1, 2]

    def test_a_run_that_drops_a_title_takes_it_off_at_once(self, conn, stub, library):
        library(1)
        _run(conn)
        _ids(conn)
        conn.execute("DELETE FROM media_items")

        _run(conn)

        assert _ids(conn) == []

    def test_ticking_other_libraries_shows_at_once(self, conn, stub, library):
        library(1)
        _run(conn)
        _ids(conn)

        conn.execute("UPDATE settings SET value = '[]' WHERE key = 'plex_libraries'")

        assert _ids(conn) == []

    def test_what_was_read_before_a_clear_is_not_held_after_it(
        self, conn, stub, library, monkeypatch
    ):
        library(1)
        _run(conn)
        real = _title_meta._build

        def deleted_meanwhile(c):
            built = real(c)
            conn.execute("DELETE FROM media_items")
            catalog.clear_library_index()
            return built

        monkeypatch.setattr(_title_meta, "_build", deleted_meanwhile)
        _ids(conn)
        monkeypatch.setattr(_title_meta, "_build", real)

        assert _ids(conn) == []


@pytest.mark.parametrize(
    ("known", "total", "ready"),
    [(90, 100, True), (89, 100, False), (3, 5, True), (2, 5, False), (0, 1, False), (0, 0, False)],
)
def test_ready_allows_a_tenth_missing_or_two_titles(known, total, ready):
    assert catalog.Coverage(known, total).ready is ready


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

    def test_who_is_billed_higher_comes_first_and_only_a_few_are_listed(self, conn, stub, library):
        cast = [20, 19, 18, 17, 16, 15, 14, 13]
        library(1, cast=cast)
        library(2, cast=cast)
        _run(conn)

        found = catalog.library_people(conn)

        assert [p["person_id"] for p in found.people] == [20, 19, 18, 17, 16, 15]
        assert [p["person_id"] for p in found.actors["action"]] == [20, 19, 18]

    def test_nobody_is_listed_before_the_library_is_filled(self, conn, stub, library):
        for n in range(1, 10):
            library(n, cast=[7])
        _run(conn)
        half = catalog.library_people(conn)
        _run(conn)

        assert (half.coverage.ready, half.people, half.actors) == (False, [], {})
        assert catalog.library_people(conn).people[0]["titles"] == 9

    def test_a_library_without_titles_is_not_ready(self, conn, stub):
        assert _coverage(conn) == (0, 0, False)
