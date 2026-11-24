"""Watch catalog: TMDB titles by genre, and people."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

import pytest
import requests

from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.watch_catalog import _discover, _explore


class Tmdb:
    """A TmdbClient that answers from ``pages``, by path; an exception is raised, and a list
    is one answer a page."""

    def __init__(self, monkeypatch) -> None:
        self.pages: dict[str, object] = {}
        self.asked: list[tuple[str, dict]] = []
        self.client = TmdbClient("token")
        monkeypatch.setattr(self.client, "_get", self._get)

    def _get(self, path, params=None):
        self.asked.append((path, params or {}))
        answer = self.pages[path]
        if isinstance(answer, list):
            answer = answer[params["page"] - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def tmdb(monkeypatch, tmp_path):
    disk_cache.init(tmp_path)
    monkeypatch.setattr(disk_cache, "sync", True)
    monkeypatch.setattr(_explore, "_tiles_asked", {})
    monkeypatch.setattr(_explore, "now_utc", lambda: datetime(2026, 6, 15, 12, tzinfo=UTC))
    return Tmdb(monkeypatch)


def _failed(status: int) -> SafeHTTPError:
    return SafeHTTPError(status_code=status, body_snippet="", url="https://tmdb.invalid")


def _list(*items, pages=1, total=None):
    return {"results": list(items), "total_pages": pages, "total_results": total or len(items)}


def _ranked(count, step, start=0):
    """Titles in both orders, *step* apart."""
    rows = []
    for number in range(start, start + count):
        place = step * number
        day = f"{2026 - place // 12:04d}-{12 - place % 12:02d}-01"
        rows.append(
            {
                "id": number + 1,
                "vote_average": 9 - place / 50,
                "release_date": day,
                "first_air_date": day,
            }
        )
    return rows


def _paged(films, shows):
    return {
        f"/discover/{kind}": [
            _list(*rows[at : at + 20], pages=-(-len(rows) // 20), total=len(rows))
            for at in range(0, len(rows), 20)
        ]
        for kind, rows in (("movie", films), ("tv", shows))
    }


def _walked(tmdb, sort, after=""):
    """All there is from *after* on, and the totals said on the way."""
    got, totals = [], []
    while after is not None:
        assert len(totals) < 20
        items, total, after = catalog.explore_page(
            tmdb.client, catalog.GENRES["action"], sort=sort, after=after
        )
        got += items
        totals.append(total)
    return got, totals


def _explore_ids(tmdb, genre="action", **kwargs):
    items, _, _ = catalog.explore_page(tmdb.client, catalog.GENRES[genre], **kwargs)
    return [(i["media_type"], i["id"]) for i in items]


class TestExplorePage:
    def test_each_kind_is_asked_for_with_its_own_genre(self, tmdb):
        tmdb.pages = {"/discover/movie": _list({"id": 1}), "/discover/tv": _list({"id": 2})}

        assert _explore_ids(tmdb, "war") == [("movie", 1), ("tv", 2)]
        assert [(p, q["with_genres"]) for p, q in tmdb.asked] == [
            ("/discover/movie", 10752),
            ("/discover/tv", 10768),
        ]

    def test_a_genre_only_films_have_asks_for_no_tv(self, tmdb):
        tmdb.pages = {"/discover/movie": _list({"id": 1})}

        assert _explore_ids(tmdb, "horror") == [("movie", 1)]
        assert _explore_ids(tmdb, "horror", media_type="tv") == []

    def test_adult_titles_are_not_asked_for_and_dropped_when_they_come(self, tmdb):
        tmdb.pages = {"/discover/movie": _list({"id": 1, "adult": True}, {"id": 2, "adult": False})}

        assert _explore_ids(tmdb, media_type="movie") == [("movie", 2)]
        assert tmdb.asked[0][1]["include_adult"] == "false"

    @pytest.mark.parametrize(
        ("kind", "sort", "decade", "expected"),
        [
            ("movie", "popular", None, {"sort_by": "popularity.desc"}),
            (
                "movie",
                "top_rated",
                "1990s",
                {
                    "sort_by": "vote_average.desc",
                    "vote_count.gte": 300,
                    "primary_release_date.gte": "1990-01-01",
                    "primary_release_date.lte": "1999-12-31",
                },
            ),
            ("tv", "top_rated", None, {"sort_by": "vote_average.desc", "vote_count.gte": 200}),
            (
                "tv",
                "newest",
                "2020s",
                {
                    "sort_by": "first_air_date.desc",
                    "vote_count.gte": 10,
                    "first_air_date.gte": "2020-01-01",
                    "first_air_date.lte": "2026-06-15",
                },
            ),
            (
                "movie",
                "newest",
                None,
                {
                    "sort_by": "primary_release_date.desc",
                    "vote_count.gte": 10,
                    "primary_release_date.lte": "2026-06-15",
                },
            ),
        ],
    )
    def test_the_sort_and_decade_reach_tmdb(self, tmdb, kind, sort, decade, expected):
        tmdb.pages = {f"/discover/{kind}": _list()}

        _explore_ids(tmdb, media_type=kind, sort=sort, decade=decade, page=3)

        [(_, params)] = tmdb.asked
        assert params == {"with_genres": params["with_genres"], "page": 3} | expected | {
            "include_adult": "false"
        }

    @pytest.mark.parametrize(
        ("sort", "expected"),
        [("popular", [1, 9, 2]), ("top_rated", [1, 2, 9]), ("newest", [2, 9, 1])],
    )
    def test_films_and_tv_take_turns_or_stand_in_order(self, tmdb, sort, expected):
        tmdb.pages = {
            "/discover/movie": _list(
                {"id": 1, "vote_average": 9.0, "release_date": "1980-01-01"},
                {"id": 2, "vote_average": 8.0, "release_date": "2025-01-01"},
            ),
            "/discover/tv": _list({"id": 9, "vote_average": 7.0, "first_air_date": "1999-01-01"}),
        }

        assert [i for _, i in _explore_ids(tmdb, sort=sort)] == expected

    @pytest.mark.parametrize("sort", ["top_rated", "newest"])
    def test_films_and_tv_stand_in_one_order_across_pages_each_title_once(self, tmdb, sort):
        films, shows = _ranked(60, 2), _ranked(40, 5)
        films[20:20] = [films[19]]  # moved to the next page between two reads
        tmdb.pages = _paged(films, shows)

        got, totals = _walked(tmdb, sort)

        titles = [(i["media_type"], i["id"]) for i in got]
        assert set(totals) == {101}
        assert len(titles) == len(set(titles)) == 100
        field = "vote_average" if sort == "top_rated" else "release_date"
        # To one decimal, as a card has it.
        order = [round(i[field], 1) if sort == "top_rated" else i[field] for i in got]
        assert order == sorted(order, reverse=True)
        # Each of TMDB's pages once.
        assert len(tmdb.asked) == 4 + 2

    @pytest.mark.parametrize("after", ["499-7,480-9", "500-1,1"])
    def test_a_page_reads_few_of_tmdb_s_pages_however_deep_it_is(self, tmdb, after):
        tmdb.pages = {
            f"/discover/{kind}": [_list(*_ranked(20, 1, page * 20), pages=500)] * 500
            for page, kind in enumerate(("movie", "tv"))
        }

        items, _, _ = catalog.explore_page(
            tmdb.client, catalog.GENRES["action"], sort="top_rated", after=after
        )

        assert len(items) == 20
        assert len(tmdb.asked) <= 4

    def test_pages_with_next_to_nothing_on_them_end_one_early_and_the_next_goes_on(self, tmdb):
        adult = _list({"id": 1, "adult": True}, pages=9)
        last = _list({"id": 5, "vote_average": 8}, pages=9)
        tmdb.pages = {"/discover/movie": [adult] * 8 + [last], "/discover/tv": [_list()]}

        items, _, after = catalog.explore_page(
            tmdb.client, catalog.GENRES["action"], sort="top_rated", after=""
        )
        assert items == []
        assert len(tmdb.asked) == _explore._MERGED_READS

        items, _, after = catalog.explore_page(
            tmdb.client, catalog.GENRES["action"], sort="top_rated", after=after
        )
        assert ([i["id"] for i in items], after) == ([5], None)

    def test_a_page_of_tmdb_s_that_is_refused_ends_one_early_and_the_next_begins_there(self, tmdb):
        films = _ranked(40, 1)
        tmdb.pages = {
            "/discover/movie": [_list(*films[:10], pages=2), _list(*films[10:], pages=2)],
            "/discover/tv": _list(*_ranked(1, 9)),
        }
        genre = catalog.GENRES["action"]
        left = [True, True]

        some, _, after = catalog.explore_page(
            tmdb.client,
            genre,
            sort="top_rated",
            after="",
            allow_fetch=lambda: bool(left) and left.pop(),
        )
        assert isinstance(after, str)
        with pytest.raises(disk_cache.Refused):
            catalog.explore_page(
                tmdb.client, genre, sort="top_rated", after=after, allow_fetch=lambda: False
            )
        rest, _ = _walked(tmdb, "top_rated", after)

        titles = [(i["media_type"], i["id"]) for i in some + rest]
        assert (len(some), len(titles), len(set(titles))) == (11, 41, 41)

    def test_a_list_that_moved_between_two_pages_loses_no_title(self, tmdb):
        films, shows = _ranked(40, 2), _ranked(1, 1)
        tmdb.pages = _paged(films, shows)
        genre = catalog.GENRES["action"]
        first, _, after = catalog.explore_page(tmdb.client, genre, sort="top_rated", after="")
        # One film comes in at the top, and the last one shown goes; both lists are read anew.
        gone = first[-1]["id"]
        films = [{"id": 900, "vote_average": 9.5}] + [f for f in films if f["id"] != gone]
        tmdb.pages = _paged(films, shows)
        shutil.rmtree(_discover._cache()._dir())

        rest, _ = _walked(tmdb, "top_rated", after)

        seen = {i["id"] for i in first + rest if i["media_type"] == "movie"}
        assert seen >= {f["id"] for f in _ranked(40, 2)}

    @pytest.mark.parametrize("fell", [(9,), (5, 9)])
    def test_titles_that_fall_down_their_page_between_two_reads_hide_none(self, tmdb, fell):
        films = _ranked(20, 2)
        shows = [{"id": 500 + n, "vote_average": 9.5} for n in range(10)]
        tmdb.pages = _paged(films, shows)
        genre = catalog.GENRES["action"]
        first, _, after = catalog.explore_page(tmdb.client, genre, sort="top_rated", after="")
        shown = {i["id"] for i in first}
        assert [i["id"] for i in first[10:]] == list(range(1, 11))
        films = [f | ({"vote_average": 1.0} if n in fell else {}) for n, f in enumerate(films)]
        tmdb.pages = _paged(sorted(films, key=lambda f: -f["vote_average"]), shows)
        shutil.rmtree(_discover._cache()._dir())

        rest, _ = _walked(tmdb, "top_rated", after)

        again = [i["id"] for i in rest]
        assert len(again) == len(set(again)) and not shown & set(again)
        assert shown | set(again) >= {f["id"] for f in films}

    def test_a_title_moved_to_the_next_page_is_in_a_reply_once(self, tmdb):
        films = _ranked(40, 1)
        films[20:20] = films[5:6]
        tmdb.pages = _paged(films, _ranked(20, 60))

        got, _ = _walked(tmdb, "top_rated")

        titles = [(i["media_type"], i["id"]) for i in got]
        assert len(titles) == len(set(titles)) == 60

    @pytest.mark.parametrize("moved", [2, 5, 19])
    def test_a_title_moved_to_the_next_page_is_in_no_later_reply(self, tmdb, moved):
        films = _ranked(40, 1)
        films[20:20] = [films[moved]]
        tmdb.pages = _paged(films, _ranked(40, 1))

        got, _ = _walked(tmdb, "top_rated")

        titles = [(i["media_type"], i["id"]) for i in got]
        assert len(titles) == len(set(titles)) == 80

    def test_by_page_each_is_in_order_by_itself(self, tmdb):
        tmdb.pages = _paged(_ranked(40, 1), _ranked(40, 1))
        genre = catalog.GENRES["action"]

        second, total, following = catalog.explore_page(
            tmdb.client, genre, sort="top_rated", page=2
        )

        assert (len(second), total, following) == (40, 80, None)
        assert {i["id"] for i in second} == set(range(21, 41))

    def test_the_list_goes_on_while_either_kind_has_a_page_and_counts_both(self, tmdb):
        tmdb.pages = {
            "/discover/movie": _list({"id": 1}, pages=1, total=1),
            "/discover/tv": _list({"id": 2}, pages=2, total=30),
        }
        genre = catalog.GENRES["action"]

        assert catalog.explore_page(tmdb.client, genre)[1:] == (31, 2)
        assert catalog.explore_page(tmdb.client, genre, page=2)[1:] == (31, None)

    def test_the_list_ends_at_the_last_page_tmdb_gives(self, tmdb):
        tmdb.pages = {"/discover/movie": _list({"id": 1}, pages=9000)}
        genre = catalog.GENRES["horror"]

        assert catalog.explore_page(tmdb.client, genre, page=499)[2] == 500
        assert catalog.explore_page(tmdb.client, genre, page=500)[2] is None

    def test_only_what_a_card_shows_is_kept(self, tmdb):
        tmdb.pages = {"/discover/movie": _list({"id": 1, "title": "T", "overview": "x" * 900})}

        [item], _, _ = catalog.explore_page(tmdb.client, catalog.GENRES["horror"])

        assert item == {"id": 1, "title": "T", "media_type": "movie"}

    def test_a_page_of_adult_titles_alone_does_not_end_the_list(self, tmdb):
        tmdb.pages = {"/discover/movie": _list({"id": 1, "adult": True}, pages=3)}

        items, _, next_page = catalog.explore_page(tmdb.client, catalog.GENRES["horror"])

        assert (items, next_page) == ([], 2)

    def test_a_genre_with_one_number_for_films_and_tv_keeps_their_lists_apart(self, tmdb):
        tmdb.pages = {"/discover/movie": _list({"id": 1}), "/discover/tv": _list({"id": 2})}
        assert catalog.GENRES["comedy"].movie == catalog.GENRES["comedy"].tv

        for _ in range(2):
            assert _explore_ids(tmdb, "comedy") == [("movie", 1), ("tv", 2)]
        assert len(tmdb.asked) == 2

    @pytest.mark.parametrize("vote", [float("nan"), float("inf"), pytest.param(10**400, id="huge")])
    def test_an_odd_rating_is_sorted_as_none(self, tmdb, vote):
        rows = [{"id": 1, "vote_average": vote}, {"id": 2, "vote_average": 5}]
        tmdb.pages = {"/discover/movie": _list(*rows), "/discover/tv": _list()}

        assert _explore_ids(tmdb, sort="top_rated") == [("movie", 2), ("movie", 1)]

    @pytest.mark.parametrize("which", [{"sort": "newest"}, {"decade": "1990s"}, {"page": 2}])
    def test_another_list_is_not_answered_from_the_cache_of_this_one(self, tmdb, which):
        tmdb.pages = {"/discover/movie": _list({"id": 1})}
        _explore_ids(tmdb, "horror")
        _explore_ids(tmdb, "horror")
        assert len(tmdb.asked) == 1

        _explore_ids(tmdb, "horror", **which)

        assert len(tmdb.asked) == 2

    @pytest.mark.parametrize(
        ("answer", "raised"),
        [
            (_failed(500), catalog.TmdbUnavailable),
            (_failed(429), catalog.TmdbBusy),
            (requests.ConnectionError("down"), catalog.TmdbUnavailable),
            ({"status_message": "odd"}, catalog.TmdbUnavailable),
        ],
    )
    def test_a_failure_is_not_taken_for_an_empty_list(self, tmdb, answer, raised):
        tmdb.pages = {"/discover/movie": answer}

        with pytest.raises(raised):
            _explore_ids(tmdb, "horror")

    def test_one_kind_failing_fails_the_page_and_the_other_is_not_asked_again(self, tmdb):
        tmdb.pages = {"/discover/movie": _list({"id": 1}), "/discover/tv": _failed(500)}
        with pytest.raises(catalog.TmdbUnavailable):
            _explore_ids(tmdb)
        tmdb.pages["/discover/tv"] = _list({"id": 2})
        disk_cache._failed.clear()

        assert _explore_ids(tmdb) == [("movie", 1), ("tv", 2)]
        assert [p for p, _ in tmdb.asked].count("/discover/movie") == 1

    def test_each_tmdb_page_is_counted_and_a_refused_one_is_not_asked_for(self, tmdb):
        tmdb.pages = {"/discover/movie": _list({"id": 1}), "/discover/tv": _list({"id": 2})}
        left = [True]

        with pytest.raises(disk_cache.Refused):
            _explore_ids(tmdb, allow_fetch=lambda: bool(left) and left.pop())

        assert [p for p, _ in tmdb.asked] == ["/discover/movie"]


class TestGenrePosters:
    def test_tiles_fill_in_the_background_and_share_no_poster(self, tmdb):
        shared = [{"id": n, "poster_path": f"/p{n}.jpg"} for n in range(60)]
        tmdb.pages = {"/discover/movie": _list(*shared), "/discover/tv": _list(*shared)}

        assert not any(catalog.genre_posters(tmdb.client).values())
        posters = catalog.genre_posters(tmdb.client)

        assert posters["action"] == [
            "/api/watch/img/tmdb/w185/p0.jpg",
            "/api/watch/img/tmdb/w185/p1.jpg",
        ]
        flat = [url for urls in posters.values() for url in urls]
        assert len(flat) == len(set(flat)) == 2 * len(catalog.GENRES)

    def test_a_cache_that_cannot_be_written_does_not_ask_for_them_on_every_call(
        self, tmdb, tmp_path
    ):
        shutil.rmtree(tmp_path / "watch_cache")
        tmdb.pages = {"/discover/movie": _list({"id": 1}), "/discover/tv": _list({"id": 2})}

        for _ in range(3):
            assert not any(catalog.genre_posters(tmdb.client).values())

        assert len(tmdb.asked) == len(catalog.GENRES)

    def test_tiles_are_asked_for_again_later(self, tmdb, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(_explore, "_clock", lambda: now[0])
        tmdb.pages = {"/discover/movie": _failed(500), "/discover/tv": _failed(500)}
        catalog.genre_posters(tmdb.client)
        tmdb.pages = {"/discover/movie": _list({"id": 1}), "/discover/tv": _list({"id": 2})}
        asked = []
        for _ in range(2):
            catalog.genre_posters(tmdb.client)
            asked.append(len(tmdb.asked))

        now[0] += _explore._TILES_PAUSE_S
        catalog.genre_posters(tmdb.client)

        # All but the one that failed, and that one in its own time.
        assert (asked, len(tmdb.asked)) == ([len(catalog.GENRES)] * 2, len(catalog.GENRES) + 1)

    def test_the_first_list_that_fails_ends_the_asking(self, tmdb):
        tmdb.pages = {"/discover/movie": _failed(500), "/discover/tv": _failed(500)}

        catalog.genre_posters(tmdb.client)

        assert len(tmdb.asked) == 1

    def test_a_list_that_failed_for_a_tile_holds_nobody_back(self, tmdb):
        tmdb.pages = {"/discover/movie": _failed(500), "/discover/tv": _failed(500)}
        catalog.genre_posters(tmdb.client)
        tmdb.pages = {"/discover/movie": _list({"id": 1}), "/discover/tv": _list({"id": 2})}

        assert _explore_ids(tmdb, "action") == [("movie", 1), ("tv", 2)]

    def test_a_list_that_cannot_be_read_leaves_a_plain_tile_and_fails_as_tmdb_does(
        self, tmdb, monkeypatch
    ):
        def fault(_):
            raise TypeError("unhashable")

        tmdb.pages = {"/discover/movie": _list({"id": 1}), "/discover/tv": _list()}
        monkeypatch.setattr(_explore, "dicts", fault)
        catalog.genre_posters(tmdb.client)

        with pytest.raises(catalog.TmdbUnavailable):
            _explore_ids(tmdb, "horror")

    def test_tmdb_down_leaves_plain_tiles(self, tmdb):
        tmdb.pages = {"/discover/movie": _failed(500), "/discover/tv": _failed(500)}

        catalog.genre_posters(tmdb.client)

        assert not any(catalog.genre_posters(tmdb.client).values())


class TestSearchPeople:
    def test_people_are_listed_and_adult_ones_and_titles_are_not(self, tmdb):
        tmdb.pages = {
            "/search/multi": {
                "results": [
                    {"id": 1, "media_type": "movie", "title": "Plain"},
                    {"id": 2, "media_type": "movie", "title": "Adult", "adult": True},
                    {"id": 3, "media_type": "person", "name": "Adult", "adult": True},
                    {
                        "id": 4,
                        "media_type": "person",
                        "name": "Mara Ellison",
                        "known_for_department": "Directing",
                        "profile_path": "/m.jpg",
                        "known_for": [
                            {"title": "Plain"},
                            {"name": "A Show"},
                            {"title": "Adult", "adult": True},
                        ],
                    },
                    {"id": "x", "media_type": "person", "name": "Malformed"},
                ]
            }
        }

        found = catalog.search(tmdb.client, "mara")

        assert [i["id"] for i in found] == [1, 4, "x"]
        assert catalog.people(found) == [
            {
                "person_id": 4,
                "name": "Mara Ellison",
                "job": "Director",
                "profile_url": "/api/watch/img/tmdb/w185/m.jpg",
                "known_for": ["Plain", "A Show"],
            }
        ]


def _person(**fields):
    return {
        "id": 7,
        "name": "Mara Ellison",
        "known_for_department": "Acting",
        "birthday": "1979-08-02",
        "combined_credits": {"cast": [], "crew": []},
    } | fields


def _credits(tmdb, cast=(), crew=(), **fields):
    tmdb.pages = {
        "/person/7": _person(combined_credits={"cast": list(cast), "crew": list(crew)}, **fields)
    }
    return catalog.person(tmdb.client, 7)["credits"]


def _movie(tmdb_id, votes=0, **fields):
    return {"id": tmdb_id, "media_type": "movie", "title": "T", "vote_count": votes} | fields


class TestPerson:
    def test_what_is_stored_has_no_plots_and_the_second_ask_is_free(self, tmdb):
        _credits(tmdb, [_movie(1, overview="x" * 900)], adult=False)

        again = catalog.person(tmdb.client, 7, allow_fetch=lambda: False)

        assert "overview" not in again["credits"][0]
        assert again["name"] == "Mara Ellison"
        assert len(tmdb.asked) == 1

    def test_a_title_stands_once_with_every_part_in_it(self, tmdb):
        [credit] = _credits(
            tmdb,
            [_movie(1, character="Edith")],
            [
                _movie(1, department="Writing", job="Writer"),
                _movie(1, department="Writing", job="Writer"),
            ],
        )

        assert (credit["role"], credit["minor"]) == ("Edith, Writer", False)

    def test_a_film_and_a_show_with_one_number_are_two_titles(self, tmdb):
        credits = _credits(tmdb, [_movie(1), {**_movie(1), "media_type": "tv", "episode_count": 9}])

        assert [c["media_type"] for c in credits] == ["movie", "tv"]

    def test_adult_and_malformed_credits_are_dropped(self, tmdb):
        credits = _credits(tmdb, [_movie(1, adult=True), _movie("x"), {"id": 3}, _movie(4)])

        assert [c["id"] for c in credits] == [4]

    def test_best_known_first_and_titles_they_only_appear_in_last(self, tmdb):
        def show(tmdb_id, votes, episodes=20, genres=(), **fields):
            return _movie(tmdb_id, votes, episode_count=episodes, genre_ids=list(genres)) | {
                "media_type": "tv",
                **fields,
            }

        credits = _credits(
            tmdb,
            [
                show(1, 9000, genres=[10767]),
                show(2, 8000, episodes=1),
                _movie(3, 7000, character="Self - Host"),
                _movie(4, 10, character="Selfish Man"),
                show(5, 50),
            ],
            [_movie(6, 99999, department="Directing", job="Director")],
        )

        assert [c["id"] for c in credits if not c["minor"]] == [5, 4]
        assert [c["id"] for c in credits if c["minor"]] == [6, 1, 2, 3]

    def test_a_director_is_known_for_what_they_directed(self, tmdb):
        credits = _credits(
            tmdb,
            [_movie(1, 9000, character="Man in Bar")],
            [_movie(2, 10, department="Directing", job="Director")],
            known_for_department="Directing",
        )

        assert [c["id"] for c in credits] == [2, 1]

    @pytest.mark.parametrize(
        ("answer", "raised"),
        [
            (_failed(404), catalog.PersonNotFound),
            (_person(adult=True), catalog.PersonNotFound),
            (_failed(429), catalog.TmdbBusy),
            (_failed(500), catalog.TmdbDown),
            ({}, catalog.TmdbUnavailable),
        ],
    )
    def test_no_such_person_and_a_failure_are_told_apart(self, tmdb, answer, raised):
        tmdb.pages = {"/person/7": answer}

        with pytest.raises(raised) as caught:
            catalog.person(tmdb.client, 7)

        assert caught.type is raised

    @pytest.mark.parametrize("now", [_failed(404), _person(adult=True)])
    def test_one_held_before_tmdb_removed_or_marked_them_is_not_served_once_refreshed(
        self, tmdb, monkeypatch, now
    ):
        tmdb.pages = {"/person/7": _person()}
        catalog.person(tmdb.client, 7)
        tmdb.pages = {"/person/7": now}
        later = disk_cache.time.time() + 8 * 86400
        monkeypatch.setattr(disk_cache.time, "time", lambda: later)
        catalog.person(tmdb.client, 7)  # old: served, and refreshed behind it

        for _ in range(2):
            with pytest.raises(catalog.PersonNotFound):
                catalog.person(tmdb.client, 7)
        assert len(tmdb.asked) == 2

    def test_odd_votes_and_genres_do_not_lose_the_person(self, tmdb):
        credits = _credits(
            tmdb,
            [
                _movie(1, float("nan")),
                _movie(2, 10**400),
                _movie(3, 5),
                _movie(4, 4) | {"media_type": "tv", "genre_ids": [[10767], {}]},
            ],
        )

        assert [c["id"] for c in credits] == [3, 4, 1, 2]

    def test_a_long_biography_ends_at_a_sentence_and_a_short_one_is_whole(self, tmdb):
        tmdb.pages = {"/person/7": _person(biography="A sentence of words. " * 60)}
        text = catalog.person(tmdb.client, 7)["biography"]
        assert 400 < len(text) <= 600
        assert text.endswith("words.")

        tmdb.pages = {"/person/8": _person(biography="Short. And")}
        assert catalog.person(tmdb.client, 8)["biography"] == "Short. And"

    def test_a_biography_without_a_place_to_end_is_cut_at_a_word(self, tmdb):
        tmdb.pages = {"/person/7": _person(biography="Born in St. Ives " + "wordy " * 200)}

        text = catalog.person(tmdb.client, 7)["biography"]

        assert 500 < len(text) <= 600
        assert text.endswith("wordy…")

    def test_a_date_that_is_none_is_left_out(self, tmdb):
        tmdb.pages = {"/person/7": _person(birthday="1979", deathday="")}

        found = catalog.person(tmdb.client, 7)

        assert (found["birthday"], found["deathday"]) == (None, None)

    @pytest.mark.parametrize(
        ("birthday", "deathday", "years"),
        [
            ("1979-06-15", None, 47),
            ("1979-06-16", None, 46),
            ("1920-03-01", "2000-02-29", 79),
            (None, "2000-01-01", None),
            ("2000-01-01", "1990-01-01", None),
        ],
    )
    def test_age_is_todays_or_the_one_at_death(self, tmdb, birthday, deathday, years):
        assert catalog.age(birthday, deathday) == years


def test_every_genre_of_tmdb_can_be_explored():
    movie = {28, 12, 16, 35, 80, 99, 18, 10751, 14, 36, 27, 10402, 9648, 10749, 878, 10770, 53}
    movie |= {10752, 37}
    tv = {10759, 16, 35, 80, 99, 18, 10751, 10762, 9648, 10763, 10764, 10765, 10766, 10767, 10768}
    tv |= {37}

    assert {g.movie for g in catalog.GENRES.values()} - {None} == movie
    assert {g.tv for g in catalog.GENRES.values()} - {None} == tv


class TestTitleLinks:
    def test_the_cast_names_its_people_and_the_genres_theirs(self, tmdb):
        fields = catalog.tmdb_fields(
            {
                "genres": [{"id": 10765, "name": "Sci-Fi & Fantasy"}, {"id": 1, "name": "Odd"}],
                "credits": {"cast": [{"id": 4, "name": "A"}, {"id": 5, "adult": True}, {}]},
            },
            "tv",
        )

        assert [c["person_id"] for c in fields["cast"]] == [4, None, None]
        assert fields["genres"] == ["Sci-Fi & Fantasy", "Odd"]
        assert fields["genre_refs"] == [{"name": "Sci-Fi & Fantasy", "genre": "science-fiction"}]

    def test_details_cached_without_the_links_are_not_read(self, tmdb):
        disk_cache.meta().get_or_fetch("tmdb:details:v2:movie:5", 9, 9, lambda: {"title": "Old"})
        tmdb.pages = {"/movie/5": {"title": "New"}}

        assert catalog.details(tmdb.client, "movie", 5)["title"] == "New"
