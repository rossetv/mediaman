"""Watch catalog: library dedupe of TMDB titles, the held Radarr/Sonarr state, TMDB
fetches through the cache, and the card and image shapes."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.watch_catalog import _arr, _discover, _library
from tests.helpers.factories import insert_media_item


class DictCache:
    """The disk cache's get_or_fetch over a dict: a failed fetch is not stored."""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    def get_or_fetch(self, key, fresh_s, stale_s, fetch, *, allow_fetch=None):
        if key in self.store:
            return self.store[key]
        if allow_fetch is not None and not allow_fetch():
            raise disk_cache.Refused(key)
        self.store[key] = fetch()
        return self.store[key]


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch):
    catalog.invalidate()
    monkeypatch.setattr(_library, "_tvdb", {})
    monkeypatch.setattr(_library, "_held", {})
    monkeypatch.setattr(_library, "sync", True)
    yield
    catalog.invalidate()


@pytest.fixture
def cache(monkeypatch):
    fake = DictCache()
    monkeypatch.setattr(_discover, "_cache", lambda: fake)
    return fake


def _radarr(movies=(), queue=()):
    return SimpleNamespace(get_movies=lambda: list(movies), get_queue=lambda: list(queue))


def _sonarr(series=(), queue=()):
    return SimpleNamespace(get_series=lambda: list(series), get_queue=lambda: list(queue))


def _view(movies=None, series=None) -> catalog.ArrView:
    return catalog.ArrView(
        {
            "radarr_movies": movies or {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": series or {},
            "sonarr_queue_tmdb_ids": set(),
        }
    )


class TestArrMemo:
    def _count_builds(self, monkeypatch, radarr):
        builds = []

        def build(conn, secret_key):
            builds.append(1)
            return radarr()

        monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", build)
        monkeypatch.setattr(_arr.arr_build, "build_sonarr_from_db", lambda *_: None)
        return builds

    def test_state_is_held_for_thirty_seconds_then_asked_again(self, monkeypatch):
        builds = self._count_builds(monkeypatch, _radarr)
        clock = [1000.0]

        catalog.arr_view(None, "k", clock=lambda: clock[0])
        clock[0] += 29
        catalog.arr_view(None, "k", clock=lambda: clock[0])
        assert len(builds) == 1

        clock[0] += 2
        catalog.arr_view(None, "k", clock=lambda: clock[0])
        assert len(builds) == 2

    def test_invalidate_makes_the_next_answer_ask_again(self, monkeypatch):
        movies: list[dict] = []
        self._count_builds(monkeypatch, lambda: _radarr(movies))
        assert catalog.arr_view(None, "k").state("movie", 7) == (None, None)

        movies.append({"tmdbId": 7, "hasFile": False, "monitored": True})
        assert catalog.arr_view(None, "k").state("movie", 7) == (None, None)
        catalog.invalidate()

        assert catalog.arr_view(None, "k").state("movie", 7) == ("queued", 0)

    def test_a_build_overtaken_by_invalidate_is_not_held(self, monkeypatch):
        def radarr():
            if len(builds) == 1:
                catalog.invalidate()  # a download lands while Radarr is being read
            return _radarr()

        builds = self._count_builds(monkeypatch, radarr)

        catalog.arr_view(None, "k")
        catalog.arr_view(None, "k")

        assert len(builds) == 2

    def _blocking_build(self, monkeypatch):
        started = threading.Event()
        release = threading.Event()

        def radarr():
            if len(builds) == 1:
                started.set()
                release.wait(5)
            return _radarr()

        builds = self._count_builds(monkeypatch, radarr)
        first = threading.Thread(target=lambda: catalog.arr_view(None, "k"), daemon=True)
        first.start()
        started.wait(5)
        return builds, first, release

    def test_callers_during_a_build_wait_for_it_instead_of_building_again(self, monkeypatch):
        builds, first, release = self._blocking_build(monkeypatch)
        waiters = [
            threading.Thread(target=lambda: catalog.arr_view(None, "k"), daemon=True)
            for _ in range(3)
        ]
        for waiter in waiters:
            waiter.start()

        release.set()
        for thread in (first, *waiters):
            thread.join(5)

        assert len(builds) == 1

    def test_a_caller_after_invalidate_does_not_wait_for_the_older_build(self, monkeypatch):
        builds, first, release = self._blocking_build(monkeypatch)
        catalog.invalidate()

        catalog.arr_view(None, "k")  # would hang on the blocked build if it waited

        assert len(builds) == 2
        release.set()
        first.join(5)

    def test_an_unreachable_radarr_leaves_sonarr_state_standing(self, monkeypatch):
        def down(*_):
            raise SafeHTTPError(status_code=503, body_snippet="", url="http://radarr.invalid")

        series = {"tmdbId": 9, "tvdbId": 90, "seasons": []}
        monkeypatch.setattr(
            _arr.arr_build,
            "build_radarr_from_db",
            lambda *_: SimpleNamespace(get_movies=down, get_queue=down),
        )
        monkeypatch.setattr(_arr.arr_build, "build_sonarr_from_db", lambda *_: _sonarr([series]))

        view = catalog.arr_view(None, "k")

        assert view.state("movie", 7) == (None, None)
        assert view.state("tv", 9) == ("queued", 0)

    def test_queue_progress_is_given_only_while_queued_or_downloading(self):
        view = catalog.ArrView(
            {
                "radarr_movies": {
                    1: {"tmdbId": 1, "hasFile": False, "monitored": True},
                    2: {"tmdbId": 2, "hasFile": True},
                },
                "radarr_queue_tmdb_ids": {1, 2},
                "radarr_queue_progress": {1: 42, 2: 100},
                "sonarr_series": {},
                "sonarr_queue_tmdb_ids": set(),
            }
        )

        assert view.state("movie", 1) == ("downloading", 42)
        assert view.state("movie", 2) == ("in_library", None)


class TestLibraryRef:
    index = catalog.LibraryIndex.build(
        [("100", "The Glasshouse", 11)], [("500", "Glass Coast", 5500)]
    )

    def test_a_movie_matches_on_its_tmdb_id(self):
        assert self.index.library_ref("movie", 11, "Another Name", _view()) == "movie-100"

    def test_a_show_matches_through_sonarrs_tvdb_id(self):
        arr = _view(series={77: {"tmdbId": 77, "tvdbId": 5500, "seasons": []}})

        assert self.index.library_ref("tv", 77, "Renamed", arr) == "show-500"

    def test_a_title_alone_matches_only_when_arr_has_it_downloaded(self):
        held = _view(movies={12: {"tmdbId": 12, "hasFile": True}})

        assert self.index.library_ref("movie", 12, "the glasshouse!", held) == "movie-100"
        # A remake TMDB knows under the same title, which Radarr does not hold.
        assert self.index.library_ref("movie", 13, "The Glasshouse", _view()) is None


class TestLibraryRefWithoutSonarr:
    def test_a_show_matches_on_the_tmdb_id_its_library_rows_carry(self):
        index = catalog.LibraryIndex.build(
            [], [("500", "Glass Coast", None)], ids={"show-500": (77, None)}
        )

        assert index.library_ref("tv", 77, "Renamed", _view()) == "show-500"

    def test_a_show_matches_through_the_tmdb_id_tmdb_maps_its_tvdb_id_to(self):
        index = catalog.LibraryIndex.build(
            [], [("500", "Glass Coast", 5500)], tmdb_for_tvdb={5500: 77}
        )

        assert index.library_ref("tv", 77, "Renamed", _view()) == "show-500"

    def test_a_show_matches_on_the_tvdb_id_tmdb_gives_it(self):
        index = catalog.LibraryIndex.build([], [("500", "Glass Coast", 5500)])

        assert index.library_ref("tv", 77, "Renamed", _view(), tvdb_id=5500) == "show-500"

    @pytest.mark.parametrize(
        ("title", "ids"),
        [("Glass Coast", {"show-500": (None, 2020)}), ("Glass Coast (2020)", {})],
    )
    def test_a_show_matches_on_title_and_year_but_not_one_of_another_year(self, title, ids):
        index = catalog.LibraryIndex.build([], [("500", title, None)], ids=ids)

        assert index.library_ref("tv", 77, "Glass Coast", _view(), year=2020) == "show-500"
        assert index.library_ref("tv", 78, "Glass Coast", _view(), year=1990) is None
        assert index.library_ref("tv", 79, "Glass Coast", _view()) is None

    def test_a_movie_matches_on_title_and_year_but_not_one_of_another_year(self):
        index = catalog.LibraryIndex.build(
            [("100", "The Glasshouse", None)], [], ids={"movie-100": (None, 2019)}
        )

        assert index.library_ref("movie", 11, "The Glasshouse", _view(), year=2019) == "movie-100"
        assert index.library_ref("movie", 12, "The Glasshouse", _view(), year=1971) is None


class TestDetails:
    RAW = {
        "title": "Glasshouse",
        "release_date": "2019-03-01",
        "credits": {
            "cast": [{"name": "Actor"}],
            "crew": [{"job": "Director", "name": "A. Director"}]
            + [{"job": "Grip", "name": f"Crew {n}"} for n in range(200)],
        },
        "release_dates": {"results": [{"iso_3166_1": f"C{n}"} for n in range(100)]},
    }

    def test_only_the_fields_the_title_view_uses_are_cached(self, cache):
        client = SimpleNamespace(details=lambda media_type, tmdb_id: self.RAW)

        answered = catalog.details(client, "movie", 5)

        (stored,) = cache.store.values()
        assert stored == answered == catalog.tmdb_fields(self.RAW, "movie")
        assert answered["director"] == "A. Director"

    def test_a_whole_payload_cached_before_is_not_read_as_the_fields(self, cache):
        cache.store["tmdb:details:movie:5"] = self.RAW
        client = SimpleNamespace(details=lambda media_type, tmdb_id: self.RAW)

        assert catalog.details(client, "movie", 5) == catalog.tmdb_fields(self.RAW, "movie")


class TestDiscoverShelves:
    def _client(self, trending, movies, tv):
        return SimpleNamespace(
            trending=lambda page: trending(),
            popular_movies=lambda page: movies(),
            popular_tv=lambda page: tv(),
        )

    def test_a_failed_row_is_left_out_and_not_cached(self, cache):
        calls = []

        def trending():
            calls.append(1)
            return []  # the client's answer to a failure

        client = self._client(trending, lambda: [{"id": 1}], lambda: [{"id": 2}])

        rows = catalog.discover_shelves(client)
        catalog.discover_shelves(client)

        assert [r[0] for r in rows] == ["popular_movies", "popular_tv"]
        assert rows[0][2] == [{"id": 1, "media_type": "movie"}]
        assert len(calls) == 2

    def test_no_row_answered_is_tmdb_unavailable(self, cache):
        client = self._client(list, list, list)

        with pytest.raises(catalog.TmdbUnavailable):
            catalog.discover_shelves(client)


class TestSearch:
    def test_a_tmdb_failure_is_not_taken_for_no_results(self, cache):
        def fail(path, params):
            raise SafeHTTPError(status_code=500, body_snippet="", url="https://tmdb.invalid")

        with pytest.raises(catalog.TmdbUnavailable):
            catalog.search(SimpleNamespace(_get=fail), "glass")
        assert cache.store == {}

    def test_the_same_query_in_another_case_is_served_from_the_cache(self, cache):
        calls = []

        def get(path, params):
            calls.append(params["query"])
            return {"results": [{"id": 1, "media_type": "movie"}]}

        client = SimpleNamespace(_get=get)
        catalog.search(client, "Glass")
        assert catalog.search(client, "GLASS") == [{"id": 1, "media_type": "movie"}]
        assert calls == ["Glass"]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(_library, "_clock", fake)
    return fake


class Finder:
    """TMDB's /find by TVDB id: *answers* in turn, an exception raised."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.asked: list[int] = []

    def find_by_tvdb(self, tvdb_id):
        self.asked.append(tvdb_id)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _down():
    return SafeHTTPError(status_code=503, body_snippet="", url="https://tmdb.invalid")


class TestTvdbLookup:
    def test_no_match_is_held_and_asked_again_only_after_its_expiry(self, clock):
        finder = Finder(None)

        assert catalog.tmdb_id_for_tvdb(finder, 5500) is None
        clock.now += _library.NO_MATCH_TTL_S - 1
        assert catalog.tmdb_id_for_tvdb(finder, 5500) is None
        assert finder.asked == [5500]

        clock.now += 2
        catalog.tmdb_id_for_tvdb(finder, 5500)
        assert finder.asked == [5500, 5500]

    def test_a_failure_is_not_held_as_no_match(self, clock):
        finder = Finder(_down(), 77)

        assert catalog.tmdb_id_for_tvdb(finder, 5500) is None
        clock.now += _library.RETRY_S + 1

        assert catalog.tmdb_id_for_tvdb(finder, 5500) == 77


class TestLibraryIndex:
    """The held index, built from a library of one show without a TMDB id of its own."""

    @pytest.fixture
    def library(self, conn):
        self.movies: list[SimpleNamespace] = []
        self.shows = [SimpleNamespace(key="500", title="Glass Coast", tvdb_id=5500)]
        self.reads = 0
        return conn

    def _movies(self, conn, libraries):
        self.reads += 1
        return list(self.movies)

    def _shows(self, conn, libraries):
        return list(self.shows)

    def _index(self, conn, client):
        return catalog.library_index(conn, client, ["1"], self._movies, self._shows)

    def test_it_is_built_once_within_its_window_and_afresh_after_it(self, library, clock):
        self._index(library, None)
        self.movies.append(SimpleNamespace(key="100", title="Brand New", tmdb_id=12))
        clock.now += _library.INDEX_TTL_S - 1

        assert self._index(library, None).movies_by_tmdb == {}
        assert self.reads == 1

        clock.now += 2
        assert self._index(library, None).movies_by_tmdb == {12: "movie-100"}

    def test_a_tvdb_id_tmdb_cannot_match_is_not_asked_again_within_its_expiry(self, library, clock):
        finder = Finder(None)

        for _ in range(3):
            self._index(library, finder)
            clock.now += _library.INDEX_TTL_S + 1

        assert finder.asked == [5500]

    def test_a_match_learnt_in_the_background_is_in_the_next_index(self, library, clock):
        finder = Finder(77)

        assert self._index(library, finder).shows_by_tmdb == {}
        assert self._index(library, finder).shows_by_tmdb == {77: "show-500"}
        assert finder.asked == [5500]

    def test_a_show_with_a_tmdb_id_of_its_own_is_not_asked_about(self, library, clock):
        insert_media_item(
            library, id="501", media_type="tv_season", show_rating_key="500", title="Season 1"
        )
        library.execute("UPDATE media_items SET tmdb_id = 77 WHERE id = '501'")
        library.commit()
        finder = Finder(78)

        assert self._index(library, finder).shows_by_tmdb == {77: "show-500"}
        assert finder.asked == []


class TestShapes:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/abc_D-1.jpg", "/api/watch/img/tmdb/w342/abc_D-1.jpg"),
            ("https://image.tmdb.org/t/p/w500/abc.png", "/api/watch/img/tmdb/w342/abc.png"),
            ("/../../etc/passwd.jpg", None),
            ("/abc.svg", None),
            ("https://evil.invalid/t/p/w500/abc.jpg", None),
            (None, None),
        ],
    )
    def test_only_plain_tmdb_image_names_reach_the_proxy(self, path, expected):
        assert catalog.tmdb_image_url(path, "w342") == expected

    def test_a_card_held_by_the_library_links_to_its_page(self):
        card = catalog.discover_card(
            ref="tmdb-tv-77",
            kind="show",
            title="Glass Coast",
            year=2020,
            poster_url=None,
            rating=None,
            library_ref="show-500",
            download_state="in_library",
            download_pct=100,
        )

        assert card["in_library"] is True
        assert card["href"] == "/watch/show/500"
        assert card["download_pct"] is None

    def test_tmdb_fields_prefer_a_trailer_and_cap_the_cast(self):
        details = {
            "title": "Glasshouse",
            "videos": {
                "results": [
                    {"site": "YouTube", "type": "Teaser", "key": "teaser00001"},
                    {"site": "Vimeo", "type": "Trailer", "key": "vimeo"},
                    {"site": "YouTube", "type": "Trailer", "key": "trailer0001"},
                ]
            },
            "credits": {"cast": [{"name": f"Actor {n}"} for n in range(20)]},
        }

        fields = catalog.tmdb_fields(details, "movie")

        assert fields["trailer_key"] == "trailer0001"
        assert len(fields["cast"]) == 12
