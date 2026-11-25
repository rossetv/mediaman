"""Tests for the Search page backend."""

from __future__ import annotations

import shutil
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.main import create_app
from mediaman.services.arr.base import ArrClient, ArrUpstreamError, SeasonsNotListed
from mediaman.services.arr.spec import RADARR_SPEC, SONARR_SPEC
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.watch_catalog import gate
from tests.helpers.factories import insert_subscriber

# NOTE: this file deliberately keeps its own ``app`` / ``authed_client``
# fixtures rather than adopting the shared ``app_factory`` / ``authed_client``
# in tests/unit/web/conftest.py. Search routes depend on the full
# ``create_app()`` lifespan: templates for ``/search``, security
# middleware (Origin header check), and the lifespan-built static
# mounts. The router-level shared fixture omits all of that.


@pytest.fixture
def app(db_path, secret_key):
    conn = init_db(str(db_path))
    set_connection(conn)
    # TMDB token required for any search call.
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) VALUES ('tmdb_read_token', 'test-token', 0, datetime('now'))"
    )
    conn.commit()
    application = create_app()
    application.state.config = MagicMock(secret_key=secret_key, data_dir=str(db_path.parent))
    application.state.db = conn
    yield application
    conn.close()


@pytest.fixture
def authed_client(app):
    from mediaman.web.auth.password_hash import create_user
    from mediaman.web.auth.session_store import create_session

    create_user(app.state.db, "admin", "password123", enforce_policy=False)
    token = create_session(app.state.db, "admin")
    client = TestClient(app)
    client.cookies.set("session_token", token)
    client.headers.update({"Origin": "http://testserver"})
    return client


@pytest.fixture
def discover_disk_cache(tmp_path, monkeypatch):
    """A disk cache in a fresh data dir, as a running app has; refreshes run inline."""
    from mediaman.services.infra import disk_cache

    monkeypatch.setattr(disk_cache, "_root", None)
    monkeypatch.setattr(disk_cache, "_failed", {})
    monkeypatch.setattr(disk_cache, "_inflight", {})
    monkeypatch.setattr(disk_cache, "sync", True)
    disk_cache.init(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_search_limiter():
    """Drop the shared search-query limiter state between tests.

    The limiter is module-level (200/day per actor) — without this
    reset, a long test run accumulates "admin" hits across cases and
    the day-cap eventually fires inside a test that knows nothing
    about it.
    """
    from mediaman.web.routes.search import _QUERY_LIMITER

    _QUERY_LIMITER.reset()
    yield
    _QUERY_LIMITER.reset()


class TestSearchEndpoint:
    def test_returns_merged_pages_filtered(self, authed_client, fake_http, fake_response):
        page1 = {
            "results": [
                {
                    "media_type": "movie",
                    "id": 1,
                    "title": "Dune",
                    "poster_path": "/d.jpg",
                    "release_date": "2021-10-01",
                    "vote_average": 8.0,
                    "popularity": 100.0,
                },
                {"media_type": "person", "id": 2, "name": "Ignored"},
            ],
        }
        page2 = {
            "results": [
                {
                    "media_type": "tv",
                    "id": 10,
                    "name": "Dune: Prophecy",
                    "poster_path": "/dp.jpg",
                    "first_air_date": "2024-11-01",
                    "vote_average": 7.5,
                    "popularity": 80.0,
                },
            ],
        }

        def handler(method, url, **kwargs):
            page = kwargs.get("params", {}).get("page", 1)
            return fake_response(json_data=page1 if page == 1 else page2)

        fake_http.handler(handler)

        resp = authed_client.get("/api/search?q=dune")
        assert resp.status_code == 200
        data = resp.json()
        titles = {r["title"] for r in data["results"]}
        assert titles == {"Dune", "Dune: Prophecy"}

        search_calls = [c for c in fake_http.calls if "search/multi" in c[1]]
        pages_requested = sorted(c[2]["params"]["page"] for c in search_calls)
        assert pages_requested == [1, 2]
        assert search_calls[0][2]["params"]["query"] == "dune"
        assert search_calls[0][2]["params"]["include_adult"] == "false"

    def test_survives_single_page_failure(self, authed_client, fake_http, fake_response):
        page1 = {
            "results": [
                {
                    "media_type": "movie",
                    "id": 1,
                    "title": "Dune",
                    "poster_path": "/d.jpg",
                    "release_date": "2021-10-01",
                    "vote_average": 8.0,
                    "popularity": 100.0,
                },
            ],
        }

        def handler(method, url, **kwargs):
            page = kwargs.get("params", {}).get("page", 1)
            if "search/multi" in url and page == 2:
                raise requests.ConnectionError("page 2 timeout")
            return fake_response(json_data=page1)

        fake_http.handler(handler)
        resp = authed_client.get("/api/search?q=dune")
        assert resp.status_code == 200
        data = resp.json()
        assert [r["title"] for r in data["results"]] == ["Dune"]

    def test_both_pages_failing_returns_502(self, authed_client, fake_http):
        def handler(method, url, **kwargs):
            raise requests.ConnectionError("down")

        fake_http.handler(handler)
        resp = authed_client.get("/api/search?q=dune")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_short_query_returns_empty_without_tmdb_call(self, authed_client, fake_http):
        resp = authed_client.get("/api/search?q=d")
        assert resp.status_code == 200
        assert resp.json() == {"results": []}
        assert fake_http.calls == []

    def test_missing_query_returns_422(self, authed_client):
        resp = authed_client.get("/api/search")
        assert resp.status_code == 422

    def test_requires_auth(self, app):
        client = TestClient(app)
        resp = client.get("/api/search?q=dune")
        assert resp.status_code == 401


class TestDiscoverEndpoint:
    @pytest.fixture(autouse=True)
    def _disk_cache(self, discover_disk_cache):
        return discover_disk_cache

    def test_returns_three_shelves(self, authed_client, fake_http, fake_response):
        trending_payload = {
            "results": [
                {
                    "media_type": "movie",
                    "id": 1,
                    "title": "Trending Movie",
                    "poster_path": "/t.jpg",
                    "release_date": "2024-01-01",
                    "vote_average": 8.0,
                    "popularity": 500.0,
                },
                {
                    "media_type": "tv",
                    "id": 2,
                    "name": "Trending Show",
                    "poster_path": "/ts.jpg",
                    "first_air_date": "2023-05-01",
                    "vote_average": 7.8,
                    "popularity": 300.0,
                },
            ],
        }
        movies_payload = {
            "results": [
                {
                    "id": 10,
                    "title": "Popular Movie",
                    "poster_path": "/m.jpg",
                    "release_date": "2024-02-01",
                    "vote_average": 7.5,
                    "popularity": 200.0,
                },
            ],
        }
        tv_payload = {
            "results": [
                {
                    "id": 20,
                    "name": "Popular Show",
                    "poster_path": "/s.jpg",
                    "first_air_date": "2024-03-01",
                    "vote_average": 8.2,
                    "popularity": 250.0,
                },
            ],
        }

        def handler(method, url, **kwargs):
            if kwargs.get("params", {}).get("page", 1) != 1:
                return fake_response(json_data={"results": []})
            if "/trending/" in url:
                return fake_response(json_data=trending_payload)
            if "/movie/popular" in url:
                return fake_response(json_data=movies_payload)
            if "/tv/popular" in url:
                return fake_response(json_data=tv_payload)
            raise AssertionError(f"unexpected url: {url}")

        fake_http.handler(handler)
        resp = authed_client.get("/api/search/discover")
        assert resp.status_code == 200
        data = resp.json()
        assert [r["title"] for r in data["trending"]] == ["Trending Movie", "Trending Show"]
        assert [r["title"] for r in data["popular_movies"]] == ["Popular Movie"]
        assert [r["media_type"] for r in data["popular_movies"]] == ["movie"]
        assert [r["title"] for r in data["popular_tv"]] == ["Popular Show"]
        assert [r["media_type"] for r in data["popular_tv"]] == ["tv"]

    def test_survives_single_shelf_failure(self, authed_client, fake_http, fake_response):
        good = {
            "results": [
                {
                    "media_type": "movie",
                    "id": 1,
                    "title": "Good",
                    "poster_path": "/g.jpg",
                    "release_date": "2024-01-01",
                    "vote_average": 8.0,
                    "popularity": 100.0,
                },
            ],
        }

        def handler(method, url, **kwargs):
            if "/tv/popular" in url:
                raise requests.ConnectionError("sonar down")
            if kwargs.get("params", {}).get("page", 1) != 1:
                return fake_response(json_data={"results": []})
            return fake_response(json_data=good)

        fake_http.handler(handler)
        resp = authed_client.get("/api/search/discover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["popular_tv"] == []
        assert data["trending"]
        assert data["popular_movies"]

    def test_caps_trending_at_21_and_filters_person(self, authed_client, fake_http, fake_response):
        trending_payload = {
            "results": (
                [
                    {
                        "media_type": "movie",
                        "id": i,
                        "title": f"M{i}",
                        "poster_path": "/p.jpg",
                        "release_date": "2024-01-01",
                        "vote_average": 8.0,
                        "popularity": 100.0,
                    }
                    for i in range(40)
                ]
                + [{"media_type": "person", "id": 999, "name": "Ignored"}]
            ),
        }

        def handler(method, url, **kwargs):
            if kwargs.get("params", {}).get("page", 1) != 1:
                return fake_response(json_data={"results": []})
            if "/trending/" in url:
                return fake_response(json_data=trending_payload)
            return fake_response(json_data={"results": []})

        fake_http.handler(handler)
        resp = authed_client.get("/api/search/discover")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["trending"]) == 21
        assert {r["media_type"] for r in data["trending"]}.issubset({"movie", "tv"})

    def test_returns_502_when_tmdb_not_configured(self, app):
        # Wipe the token set by the fixture.
        from mediaman.web.auth.password_hash import create_user
        from mediaman.web.auth.session_store import create_session

        app.state.db.execute("DELETE FROM settings WHERE key='tmdb_read_token'")
        app.state.db.commit()
        create_user(app.state.db, "admin", "password123", enforce_policy=False)
        token = create_session(app.state.db, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)
        resp = client.get("/api/search/discover")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_requires_auth(self, app):
        client = TestClient(app)
        resp = client.get("/api/search/discover")
        assert resp.status_code == 401


class TestFetchDiscoverShelf:
    """``_fetch_discover_shelf`` (called by the ``api_discover`` handler) keeps each shelf
    page in the disk cache: hit/miss, the ``media_type`` stamp on a fetched page, stale
    pages served then refreshed, failures left uncached and a restart keeping the cache."""

    @pytest.fixture(autouse=True)
    def _disk_cache(self, discover_disk_cache):
        return discover_disk_cache

    def test_cache_miss_fetches_and_caches(self):
        from mediaman.services.infra import disk_cache
        from mediaman.web.routes.search._enrichment import _fetch_discover_shelf

        calls = []

        def fetch_fn(page):
            calls.append(page)
            return [{"id": 1, "title": "X"}]

        result = _fetch_discover_shelf("trending", fetch_fn, None, 1)
        assert result == [{"id": 1, "title": "X"}]
        assert calls == [1]
        # The page is now cached under the shelf+page key Watch's discover rows also use.
        never = MagicMock(side_effect=AssertionError("fetched"))
        assert disk_cache.meta().get_or_fetch("tmdb:discover:trending:1", 60, 60, never) == result

    def test_cache_hit_within_ttl_skips_fetch(self):
        from mediaman.web.routes.search._enrichment import _fetch_discover_shelf

        calls = []

        def fetch_fn(page):
            calls.append(page)
            return [{"id": 99}]

        first = _fetch_discover_shelf("popular_tv", fetch_fn, "tv", 2)
        second = _fetch_discover_shelf("popular_tv", fetch_fn, "tv", 2)
        # Second call served from cache - fetch_fn invoked exactly once.
        assert calls == [2]
        assert first == second

    def test_pages_of_one_shelf_are_cached_apart(self):
        from mediaman.web.routes.search._enrichment import _fetch_discover_shelf

        calls = []

        def fetch_fn(page):
            calls.append(page)
            return [{"id": page}]

        assert _fetch_discover_shelf("trending", fetch_fn, None, 1) == [{"id": 1}]
        assert _fetch_discover_shelf("trending", fetch_fn, None, 2) == [{"id": 2}]
        assert calls == [1, 2]

    def test_injects_media_type_on_miss(self):
        from mediaman.web.routes.search._enrichment import _fetch_discover_shelf

        def fetch_fn(page):
            return [{"id": 1}, {"id": 2}]

        # The popular-movies/TV TMDB endpoints omit media_type; the helper
        # stamps it onto every item on a cache miss.
        result = _fetch_discover_shelf("popular_movies", fetch_fn, "movie", 1)
        assert all(item["media_type"] == "movie" for item in result)

    def test_no_media_type_injection_when_none(self):
        from mediaman.web.routes.search._enrichment import _fetch_discover_shelf

        def fetch_fn(page):
            return [{"id": 1}]

        # trending results already carry media_type - None means "leave alone".
        result = _fetch_discover_shelf("trending", fetch_fn, None, 1)
        assert "media_type" not in result[0]

    def test_stale_page_is_served_then_refreshed(self, monkeypatch):
        from mediaman.web.routes.search import _enrichment
        from mediaman.web.routes.search._enrichment import _fetch_discover_shelf

        answers = iter([[{"id": 1}], [{"id": 2}]])
        fetch_fn = MagicMock(side_effect=lambda page: next(answers))
        _fetch_discover_shelf("trending", fetch_fn, None, 1)

        monkeypatch.setattr(_enrichment, "_DISCOVER_FRESH_S", 0)
        served = _fetch_discover_shelf("trending", fetch_fn, None, 1)
        monkeypatch.setattr(_enrichment, "_DISCOVER_FRESH_S", 3600)
        after = _fetch_discover_shelf("trending", fetch_fn, None, 1)

        assert served == [{"id": 1}]
        assert after == [{"id": 2}]
        assert fetch_fn.call_count == 2

    def test_empty_answer_is_not_cached_and_backs_off(self, discover_disk_cache):
        from mediaman.web.routes.search._enrichment import _fetch_discover_shelf

        fetch_fn = MagicMock(return_value=[])
        assert _fetch_discover_shelf("trending", fetch_fn, None, 1) == []
        assert _fetch_discover_shelf("trending", fetch_fn, None, 1) == []

        # Nothing was written, and the second call did not ask TMDB again (60 s back-off).
        assert fetch_fn.call_count == 1
        assert not list((discover_disk_cache / "watch_cache" / "meta").iterdir())

    def test_page_survives_a_restart(self, discover_disk_cache, monkeypatch):
        from mediaman.services.infra import disk_cache
        from mediaman.web.routes.search._enrichment import _fetch_discover_shelf

        _fetch_discover_shelf("trending", lambda page: [{"id": 7}], None, 1)

        # A new process: no root, no in-flight or failure memory, same data dir.
        monkeypatch.setattr(disk_cache, "_root", None)
        monkeypatch.setattr(disk_cache, "_failed", {})
        monkeypatch.setattr(disk_cache, "_inflight", {})
        disk_cache.init(discover_disk_cache)

        never = MagicMock(side_effect=AssertionError("fetched"))
        assert _fetch_discover_shelf("trending", never, None, 1) == [{"id": 7}]


class TestEnrichRatingsHelpers:
    """Coverage for the helpers behind ``_stamp_omdb_ratings``: the cache
    hit/miss partition, the per-group OMDb
    fetch, and the budgeted ``as_completed`` drain. These exercise the
    subtle bits — the TTL cutoff, the
    "a bad fetch must not abort the fan-out" degradation, and the
    "drop incomplete, still keep the completed misses" timeout path."""

    @pytest.fixture(autouse=True)
    def _no_pause(self, monkeypatch):
        from mediaman.web.routes.search import _enrichment

        monkeypatch.setattr(_enrichment, "_paused_until", 0.0)

    @staticmethod
    def _done(tmdb_id, data):
        from concurrent.futures import Future

        fut: Future = Future()
        fut.set_result(((tmdb_id, "movie"), [{"tmdb_id": tmdb_id}], data))
        return fut

    @staticmethod
    def _pending(tmdb_id):
        from concurrent.futures import Future

        return Future()

    @staticmethod
    def _drain(futures, *, overrun=False):
        from mediaman.web.routes.search import _enrichment

        def fake_as_completed(fs, timeout=None):
            yield from (f for f in fs if f.done())
            if overrun:
                raise TimeoutError

        with patch.object(_enrichment, "as_completed", fake_as_completed):
            _enrichment._drain_futures(futures, now_iso="2026-05-14T00:00:00")
        return _enrichment._paused_until

    def test_one_bad_title_among_answers_does_not_pause(self):
        futs = [self._done(1, {"imdb": "8.0"}), self._done(2, None), self._done(3, None)]
        assert self._drain(futs) == 0.0

    def test_every_lookup_raising_pauses(self):
        from concurrent.futures import Future

        from mediaman.services.infra import SafeHTTPError

        futs: list[Future] = []
        for _ in range(3):
            fut: Future = Future()
            fut.set_exception(SafeHTTPError(status_code=500, body_snippet="x", url="http://omdb"))
            futs.append(fut)

        assert self._drain(futs) > 0.0

    def test_a_budget_overrun_with_some_answers_does_not_pause(self):
        futs = [self._done(1, {"imdb": "8.0"}), self._pending(2), self._pending(3)]
        assert self._drain(futs, overrun=True) == 0.0

    def test_a_budget_overrun_with_no_answer_pauses(self):
        assert self._drain([self._pending(i) for i in range(3)], overrun=True) > 0.0

    def test_fewer_than_three_misses_without_an_answer_do_not_pause(self):
        assert self._drain([self._done(1, None), self._done(2, None)]) == 0.0
        assert self._drain([self._pending(1)], overrun=True) == 0.0

    def test_every_lookup_giving_no_answer_pauses(self):
        assert self._drain([self._done(i, None) for i in range(3)]) > 0.0

    def _row(self, tmdb_id, media_type, *, fetched_at, rt="80%", imdb="7.5"):
        from mediaman.web.repository.search import RatingsCacheRow

        return RatingsCacheRow(
            tmdb_id=tmdb_id,
            media_type=media_type,
            imdb_rating=imdb,
            rt_rating=rt,
            metascore="74",
            fetched_at=fetched_at,
        )

    def test_partition_applies_fresh_cache_hit_in_place(self):
        from mediaman.web.routes.search._enrichment import _partition_cached

        item = {"tmdb_id": 1, "media_type": "movie", "title": "Dune"}
        by_key = {(1, "movie"): [item]}
        rows = [self._row(1, "movie", fetched_at="2026-05-01T00:00:00")]

        misses = _partition_cached(by_key, rows, cutoff="2026-04-01T00:00:00")

        # Fresh hit — applied in place, not returned as a miss.
        assert misses == []
        assert item["rt_rating"] == "80%"
        assert item["imdb_rating"] == "7.5"

    def test_partition_queues_a_stale_row_as_a_miss_and_still_shows_its_ratings(self):
        from mediaman.web.routes.search._enrichment import _partition_cached

        item = {"tmdb_id": 2, "media_type": "tv", "title": "Arcane"}
        by_key = {(2, "tv"): [item]}
        # Row predates the cutoff — a miss to ask OMDb about again, yet what it holds shows.
        rows = [self._row(2, "tv", fetched_at="2026-01-01T00:00:00")]

        misses = _partition_cached(by_key, rows, cutoff="2026-04-01T00:00:00")

        assert misses == [((2, "tv"), [item])]
        assert item["rt_rating"] == "80%"
        assert item["imdb_rating"] == "7.5"

    def test_partition_treats_absent_row_as_miss(self):
        from mediaman.web.routes.search._enrichment import _partition_cached

        item = {"tmdb_id": 3, "media_type": "movie", "title": "Solaris"}
        by_key = {(3, "movie"): [item]}

        misses = _partition_cached(by_key, rows=[], cutoff="2026-04-01T00:00:00")

        assert misses == [((3, "movie"), [item])]

    def test_fetch_for_group_returns_omdb_data(self):
        from mediaman.web.routes.search import _enrichment
        from mediaman.web.routes.search._enrichment import _fetch_ratings_for_group

        group = [{"tmdb_id": 1, "media_type": "movie", "title": "Dune", "year": 2021}]
        with patch.object(
            _enrichment, "lookup_ratings", return_value={"rt": "83%", "imdb": "8.0"}
        ) as mock_fetch:
            key, returned_group, data = _fetch_ratings_for_group(
                ((1, "movie"), group), omdb_key="k"
            )

        assert key == (1, "movie")
        assert returned_group is group
        assert data == {"rt": "83%", "imdb": "8.0"}
        # The OMDb key is threaded through explicitly, not closed over.
        assert mock_fetch.call_args.kwargs["omdb_key"] == "k"

    def test_fetch_for_group_degrades_to_no_answer_on_failure(self):
        from mediaman.services.infra import SafeHTTPError
        from mediaman.web.routes.search import _enrichment
        from mediaman.web.routes.search._enrichment import _fetch_ratings_for_group

        group = [{"tmdb_id": 9, "media_type": "movie", "title": "Boom", "year": 2000}]
        with patch.object(
            _enrichment,
            "lookup_ratings",
            side_effect=SafeHTTPError(status_code=500, body_snippet="x", url="http://omdb"),
        ):
            key, returned_group, data = _fetch_ratings_for_group(
                ((9, "movie"), group), omdb_key="k"
            )

        # A single bad lookup must not raise; it is no answer, which is not "no ratings".
        assert key == (9, "movie")
        assert data is None

    def _stale_search(self, conn, monkeypatch, lookup):
        from datetime import timedelta
        from unittest.mock import MagicMock

        from mediaman.core.time import now_utc
        from mediaman.web.routes.search import _enrichment

        monkeypatch.setattr(_enrichment, "_paused_until", 0.0)
        monkeypatch.setattr(_enrichment, "get_db", lambda: conn)
        monkeypatch.setattr(_enrichment, "get_omdb_key", lambda *_a: "k")
        monkeypatch.setattr(_enrichment, "lookup_ratings", lookup)
        conn.executemany(
            "INSERT INTO ratings_cache (tmdb_id, media_type, imdb_rating, rt_rating, metascore,"
            " fetched_at) VALUES (?, 'movie', '7.5', '80%', '74', ?)",
            [(i, (now_utc() - timedelta(days=90)).isoformat()) for i in (1, 2, 3)],
        )
        conn.commit()
        request = MagicMock()
        request.app.state.config.secret_key = "s"

        def search():
            items = [
                {"tmdb_id": i, "media_type": "movie", "title": "Dune", "year": 2021}
                for i in (1, 2, 3)
            ]
            _enrichment._stamp_omdb_ratings(items, request)
            return items[0]

        return search

    def test_search_shows_a_stale_row_when_omdb_gives_no_answer(self, conn, monkeypatch):
        search = self._stale_search(conn, monkeypatch, lambda *_a, **_k: None)

        item = search()

        assert item["rt_rating"] == "80%"
        assert item["imdb_rating"] == "7.5"

    def test_search_shows_a_stale_row_when_there_is_no_omdb_key(self, conn, monkeypatch):
        from mediaman.web.routes.search import _enrichment

        search = self._stale_search(conn, monkeypatch, lambda *_a, **_k: {"imdb": "1.0"})
        monkeypatch.setattr(_enrichment, "get_omdb_key", lambda *_a: None)

        assert search()["imdb_rating"] == "7.5"

    def test_after_no_answer_a_search_does_not_ask_omdb_again_for_a_while(self, conn, monkeypatch):
        asked = []
        search = self._stale_search(conn, monkeypatch, lambda *_a, **_k: asked.append(1) or None)

        search()
        second = search()

        assert len(asked) == 3
        assert second["imdb_rating"] == "7.5"

    def test_the_pause_after_no_answer_ends(self, conn, monkeypatch):
        from mediaman.web.routes.search import _enrichment

        asked = []
        search = self._stale_search(conn, monkeypatch, lambda *_a, **_k: asked.append(1) or None)
        search()
        monkeypatch.setattr(_enrichment, "_paused_until", 0.0)
        search()

        assert len(asked) == 6

    def test_drain_futures_writes_nothing_for_a_lookup_that_got_no_answer(self):
        from concurrent.futures import Future

        from mediaman.web.routes.search._enrichment import _drain_futures

        group = [{"tmdb_id": 1, "media_type": "movie", "title": "Dune"}]
        fut: Future = Future()
        fut.set_result(((1, "movie"), group, None))

        assert _drain_futures([fut], now_iso="2026-05-14T00:00:00") == []

    def test_drain_futures_applies_results_and_returns_writes(self):
        from concurrent.futures import Future

        from mediaman.web.routes.search._enrichment import _drain_futures

        group = [{"tmdb_id": 1, "media_type": "movie", "title": "Dune"}]
        fut: Future = Future()
        fut.set_result(((1, "movie"), group, {"rt": "83%", "imdb": "8.0", "metascore": "74"}))

        writes = _drain_futures([fut], now_iso="2026-05-14T00:00:00")

        # Ratings applied to the group in place...
        assert group[0]["rt_rating"] == "83%"
        assert group[0]["imdb_rating"] == "8.0"
        # ...and the cache-write tuple is returned for the batch upsert.
        assert writes == [(1, "movie", "8.0", "83%", "74", "2026-05-14T00:00:00")]

    def test_drain_futures_keeps_completed_when_budget_exhausted(self):
        """The subtle degradation path: when ``as_completed`` raises
        ``TimeoutError`` mid-iteration, futures already drained still land
        in ``pending_writes`` so the completed misses are written and warm
        for the next call — only the incomplete ones are dropped."""
        from concurrent.futures import Future

        from mediaman.web.routes.search import _enrichment
        from mediaman.web.routes.search._enrichment import _drain_futures

        done_group = [{"tmdb_id": 1, "media_type": "movie", "title": "Done"}]
        done_fut: Future = Future()
        done_fut.set_result(((1, "movie"), done_group, {"rt": "90%", "imdb": "9.0"}))
        stuck_fut: Future = Future()  # never resolves

        def fake_as_completed(futures, timeout=None):
            # Yield the finished future, then blow the budget — exactly
            # what the real as_completed does when a future is still pending.
            yield done_fut
            raise TimeoutError

        with patch.object(_enrichment, "as_completed", fake_as_completed):
            writes = _drain_futures([done_fut, stuck_fut], now_iso="2026-05-14T00:00:00")

        # The completed miss is still written despite the timeout.
        assert writes == [(1, "movie", "9.0", "90%", None, "2026-05-14T00:00:00")]
        assert done_group[0]["rt_rating"] == "90%"


def _tmdb_movie_payload():
    return {
        "id": 438631,
        "title": "Dune",
        "release_date": "2021-10-22",
        "tagline": "Fear is the mind-killer.",
        "overview": "A noble family...",
        "poster_path": "/p.jpg",
        "backdrop_path": "/b.jpg",
        "runtime": 155,
        "genres": [{"name": "Sci-Fi"}, {"name": "Adventure"}],
        "vote_average": 8.0,
        "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "abc123"}]},
        "credits": {
            "crew": [{"job": "Director", "name": "Denis Villeneuve"}],
            "cast": [{"name": "Timothée Chalamet", "character": "Paul", "profile_path": "/t.jpg"}],
        },
    }


def _tmdb_tv_payload():
    return {
        "id": 12345,
        "name": "Arcane",
        "first_air_date": "2021-11-06",
        "tagline": "Welcome to the Playground.",
        "overview": "Amid the stark discord...",
        "poster_path": "/a.jpg",
        "backdrop_path": "/ab.jpg",
        "episode_run_time": [42],
        "genres": [{"name": "Animation"}],
        "vote_average": 8.8,
        "created_by": [{"name": "Christian Linke"}],
        "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "vid123"}]},
        "credits": {
            "cast": [{"name": "Hailee Steinfeld", "character": "Vi", "profile_path": "/h.jpg"}]
        },
        "seasons": [
            {"season_number": 0, "name": "Specials", "episode_count": 5, "air_date": "2021-11-01"},
            {"season_number": 1, "name": "Season 1", "episode_count": 9, "air_date": "2021-11-06"},
            {"season_number": 2, "name": "Season 2", "episode_count": 9, "air_date": "2024-11-09"},
        ],
    }


class TestDetailEndpoint:
    @patch("mediaman.web.routes.search.detail.fetch_ratings", return_value={})
    def test_an_adult_title_is_not_found(self, _ratings, authed_client, fake_http, fake_response):
        fake_http.default(fake_response(json_data=_tmdb_movie_payload() | {"adult": True}))

        resp = authed_client.get("/api/search/detail/movie/438631")

        assert (resp.status_code, resp.json()["error"]) == (404, "not_found")

    @pytest.mark.parametrize("date", ["²⁰¹⁹-01-01", 2019, ["2019"], "20-1-1"])
    @pytest.mark.parametrize("kind", ["movie", "tv"])
    @patch("mediaman.web.routes.search.detail.fetch_ratings", return_value={})
    def test_an_odd_date_is_no_year(
        self, _ratings, authed_client, fake_http, fake_response, kind, date
    ):
        payload = _tmdb_tv_payload() if kind == "tv" else _tmdb_movie_payload()
        payload |= {"release_date": date, "first_air_date": date, "vote_average": float("nan")}
        for season in payload.get("seasons", []):
            season["air_date"] = date
        fake_http.default(fake_response(json_data=payload))

        data = authed_client.get(f"/api/search/detail/{kind}/1").json()

        assert (data["year"], data["rating_tmdb"]) == (None, None)
        assert [s["year"] for s in data.get("seasons", [])] == (
            [None, None] if kind == "tv" else []
        )

    @patch("mediaman.web.routes.search.detail.fetch_ratings")
    def test_movie_detail_shape(self, mock_ratings, authed_client, fake_http, fake_response):
        fake_http.default(fake_response(json_data=_tmdb_movie_payload()))
        mock_ratings.return_value = {"imdb": "8.0", "rt": "83%", "metascore": "74"}
        resp = authed_client.get("/api/search/detail/movie/438631")
        assert resp.status_code == 200
        data = resp.json()
        assert data["media_type"] == "movie"
        assert data["title"] == "Dune"
        assert data["year"] == 2021
        assert data["runtime"] == 155
        assert data["director"] == "Denis Villeneuve"
        assert data["trailer_key"] == "abc123"
        assert data["rating_imdb"] == "8.0"
        assert data["rating_rt"] == "83%"
        assert data["rating_metascore"] == "74"
        assert "seasons" not in data
        cast0 = data["cast"][0]
        assert cast0["profile_url"].endswith("/t.jpg")

    @patch("mediaman.web.routes.search.detail.fetch_ratings")
    def test_tv_detail_filters_season_zero(
        self, mock_ratings, authed_client, fake_http, fake_response
    ):
        fake_http.default(fake_response(json_data=_tmdb_tv_payload()))
        mock_ratings.return_value = {}
        resp = authed_client.get("/api/search/detail/tv/12345")
        assert resp.status_code == 200
        data = resp.json()
        assert data["runtime"] == 42
        season_nums = [s["season_number"] for s in data["seasons"]]
        assert season_nums == [1, 2]
        assert data["sonarr_tracked"] is False

    @patch("mediaman.web.routes.search.detail.fetch_ratings")
    def test_tv_detail_marks_in_library_seasons(
        self, mock_ratings, authed_client, fake_http, fake_response
    ):
        fake_http.default(fake_response(json_data=_tmdb_tv_payload()))
        mock_ratings.return_value = {}
        with patch("mediaman.web.routes.search.detail._fetch_sonarr_series_detail") as mock_cache:
            mock_cache.return_value = {"tracked": True, "seasons_in_library": {1}}
            resp = authed_client.get("/api/search/detail/tv/12345")
        data = resp.json()
        assert data["sonarr_tracked"] is True
        by_num = {s["season_number"]: s["in_library"] for s in data["seasons"]}
        assert by_num == {1: True, 2: False}

    @pytest.mark.parametrize(("kind", "service"), [("movie", "radarr"), ("tv", "sonarr")])
    @patch("mediaman.web.routes.search.detail.fetch_ratings", return_value={})
    def test_detail_opens_when_the_arr_answers_with_the_wrong_shape(
        self, _ratings, authed_client, fake_http, fake_response, kind, service
    ):
        payload = _tmdb_movie_payload() if kind == "movie" else _tmdb_tv_payload()
        fake_http.default(fake_response(json_data=payload))
        arr = ArrClient(RADARR_SPEC if kind == "movie" else SONARR_SPEC, "http://arr", "k")
        with patch(f"mediaman.web.routes.search.detail.build_{service}_from_db", return_value=arr):
            resp = authed_client.get(f"/api/search/detail/{kind}/1")
        assert resp.status_code == 200
        assert resp.json()["download_state"] is None

    @patch("mediaman.web.routes.search.detail.fetch_ratings", return_value={})
    def test_tv_detail_knows_a_held_show_when_the_lookup_answers_with_the_wrong_shape(
        self, _ratings, authed_client, fake_http, fake_response
    ):
        fake_http.default(fake_response(json_data=_tmdb_tv_payload()))
        sonarr = MagicMock()
        sonarr.get_queue.return_value = []
        sonarr.get_series.return_value = [
            {
                "tmdbId": 12345,
                "tvdbId": 9,
                "seasons": [{"seasonNumber": 1, "statistics": {"episodeFileCount": 9}}],
            }
        ]
        sonarr.lookup_series_by_tmdb.side_effect = ArrUpstreamError("not a list")
        with patch("mediaman.web.routes.search.detail.build_sonarr_from_db", return_value=sonarr):
            data = authed_client.get("/api/search/detail/tv/12345").json()
        assert data["sonarr_tracked"] is True
        assert [s["in_library"] for s in data["seasons"]] == [True, False]

    def test_rejects_invalid_media_type(self, authed_client):
        resp = authed_client.get("/api/search/detail/foo/1")
        assert resp.status_code == 400

    @patch("mediaman.web.routes.search.detail.fetch_ratings")
    def test_movie_detail_handles_missing_director_name(
        self, mock_ratings, authed_client, fake_http, fake_response
    ):
        payload = _tmdb_movie_payload()
        payload["credits"]["crew"] = [{"job": "Director"}]  # no 'name' key
        fake_http.default(fake_response(json_data=payload))
        mock_ratings.return_value = {}
        resp = authed_client.get("/api/search/detail/movie/438631")
        assert resp.status_code == 200
        assert resp.json()["director"] is None


class TestSearchPage:
    def test_authed_renders_200(self, authed_client):
        resp = authed_client.get("/search")
        assert resp.status_code == 200
        assert "Search" in resp.text

    def test_unauthed_redirects_to_login(self, app):
        client = TestClient(app)
        resp = client.get("/search", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login?next=%2Fsearch"


class TestDownloadEndpoint:
    @pytest.fixture(autouse=True)
    def _clear_limiters(self):
        from mediaman.web.routes.search import (
            _DOWNLOAD_ADMIN_LIMITER,
            _DOWNLOAD_IP_LIMITER,
            _download_dedup,
        )

        _DOWNLOAD_ADMIN_LIMITER.reset()
        _DOWNLOAD_IP_LIMITER.reset()
        _download_dedup.clear()

    @patch("mediaman.web.routes.search.download.build_radarr_from_db")
    def test_movie_adds_to_radarr(self, mock_build_radarr, authed_client):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = None
        radarr.add_movie.return_value = {"id": 1, "title": "Dune"}
        mock_build_radarr.return_value = radarr
        resp = authed_client.post(
            "/api/search/download",
            json={
                "media_type": "movie",
                "tmdb_id": 438631,
                "title": "Dune",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        radarr.add_movie.assert_called_once_with(438631, "Dune")

    @patch("mediaman.web.routes.search.download.build_radarr_from_db")
    def test_movie_already_in_radarr_blocked(self, mock_build_radarr, authed_client):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {
            "id": 1,
            "tmdbId": 438631,
            "monitored": True,
        }
        mock_build_radarr.return_value = radarr
        resp = authed_client.post(
            "/api/search/download",
            json={
                "media_type": "movie",
                "tmdb_id": 438631,
                "title": "Dune",
            },
        )
        assert resp.status_code == 409
        assert resp.json()["ok"] is False
        assert resp.json()["error"] == "'Dune' is already in Radarr"
        radarr.add_movie.assert_not_called()
        radarr.remonitor_movie.assert_not_called()

    @patch("mediaman.web.routes.search.download.build_radarr_from_db")
    def test_movie_unmonitored_in_radarr_remonitors(self, mock_build_radarr, authed_client):
        """Re-pressing Download on an abandoned movie remonitors it.

        Regression: after auto-abandon, the search modal showed a
        disabled "Queued" button and the API rejected the click with
        409 — the user couldn't re-download a movie they had abandoned.
        Now the endpoint detects the unmonitored entry and calls
        ``remonitor_movie`` (which also triggers a fresh search).
        """
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {
            "id": 77,
            "tmdbId": 438631,
            "monitored": False,
        }
        mock_build_radarr.return_value = radarr
        resp = authed_client.post(
            "/api/search/download",
            json={
                "media_type": "movie",
                "tmdb_id": 438631,
                "title": "Dune",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        radarr.remonitor_movie.assert_called_once_with(77)
        radarr.add_movie.assert_not_called()

    @patch("mediaman.web.routes.search.download.build_radarr_from_db")
    def test_a_failed_remonitor_is_a_502_not_already_in_radarr(
        self, mock_build_radarr, authed_client
    ):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 77, "monitored": False}
        radarr.remonitor_movie.side_effect = SafeHTTPError(409, "refused", "radarr/m")
        mock_build_radarr.return_value = radarr

        resp = authed_client.post(
            "/api/search/download",
            json={"media_type": "movie", "tmdb_id": 438631, "title": "Dune"},
        )

        assert resp.status_code == 502
        assert resp.json()["error"] == "Failed to add to Radarr"
        radarr.unmonitor_movie.assert_called_once_with(77, max_retries=1)

    @patch("mediaman.web.routes.search.download.build_sonarr_from_db")
    def test_tv_all_seasons_flow(self, mock_build_sonarr, authed_client):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999, "title": "BB"}
        sonarr.get_series.return_value = []
        sonarr.add_series.return_value = {"id": 42}
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post(
            "/api/search/download",
            json={
                "media_type": "tv",
                "tmdb_id": 12345,
                "title": "BB",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        sonarr.add_series.assert_called_once_with(999, "BB")
        sonarr.add_series_with_seasons.assert_not_called()

    @patch("mediaman.web.routes.search.download.build_sonarr_from_db")
    def test_tv_selective_seasons_flow(self, mock_build_sonarr, authed_client):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999, "title": "BB"}
        sonarr.get_series.return_value = []
        sonarr.add_series_with_seasons.return_value = {"id": 42}
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post(
            "/api/search/download",
            json={
                "media_type": "tv",
                "tmdb_id": 12345,
                "title": "BB",
                "monitored_seasons": [1, 2, 3],
                "search_seasons": [2, 3],
            },
        )
        assert resp.status_code == 200
        # Season 1 is monitored but not asked to be searched, and Sonarr searches what it
        # monitors, so only the searched seasons are sent.
        sonarr.add_series_with_seasons.assert_called_once_with(999, "BB", [2, 3])

    @patch("mediaman.web.routes.search.download.build_sonarr_from_db")
    def test_tv_a_picked_season_sonarr_does_not_list_is_refused_by_name(
        self, mock_build_sonarr, authed_client
    ):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999, "title": "BB"}
        sonarr.get_series.return_value = []
        sonarr.add_series_with_seasons.side_effect = SeasonsNotListed([2])
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post(
            "/api/search/download",
            json={"media_type": "tv", "tmdb_id": 12345, "title": "BB", "monitored_seasons": [2]},
        )
        assert resp.status_code == 422
        assert resp.json()["error"] == "Season 2 isn't in Sonarr's listing."
        assert resp.json()["ok"] is False

    @patch("mediaman.web.routes.search.download.build_sonarr_from_db")
    def test_tv_search_seasons_alone_add_only_those_seasons_not_the_whole_show(
        self, mock_build_sonarr, authed_client
    ):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999, "title": "BB"}
        sonarr.get_series.return_value = []
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post(
            "/api/search/download",
            json={"media_type": "tv", "tmdb_id": 12345, "title": "BB", "search_seasons": [1]},
        )
        assert resp.status_code == 200
        sonarr.add_series_with_seasons.assert_called_once_with(999, "BB", [1])
        sonarr.add_series.assert_not_called()

    @patch("mediaman.web.routes.search.download.build_sonarr_from_db")
    def test_tv_empty_monitored_seasons_with_nothing_to_search_is_refused(
        self, mock_build_sonarr, authed_client
    ):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999}
        sonarr.get_series.return_value = []
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post(
            "/api/search/download",
            json={"media_type": "tv", "tmdb_id": 12345, "title": "BB", "monitored_seasons": []},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "Pick at least one season"
        sonarr.add_series.assert_not_called()
        sonarr.add_series_with_seasons.assert_not_called()

    @pytest.mark.parametrize(
        ("picked", "missing", "message"),
        [
            ([1, 2], [2], "Season 2 isn't in Sonarr's listing. Untick it to add the rest."),
            (
                [1, 2, 3],
                [2, 3],
                "Seasons 2 and 3 aren't in Sonarr's listing. Untick them to add the rest.",
            ),
        ],
    )
    @patch("mediaman.web.routes.search.download.build_sonarr_from_db")
    def test_tv_when_other_picks_are_listed_the_refusal_says_to_untick_the_missing(
        self, mock_build_sonarr, authed_client, picked, missing, message
    ):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999, "title": "BB"}
        sonarr.get_series.return_value = []
        sonarr.add_series_with_seasons.side_effect = SeasonsNotListed(missing)
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post(
            "/api/search/download",
            json={"media_type": "tv", "tmdb_id": 12345, "title": "BB", "monitored_seasons": picked},
        )
        assert resp.status_code == 422
        assert resp.json()["error"] == message

    @patch("mediaman.web.routes.search.download.build_sonarr_from_db")
    def test_tv_monitored_seasons_alone_are_the_seasons_added(
        self, mock_build_sonarr, authed_client
    ):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999, "title": "BB"}
        sonarr.get_series.return_value = []
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post(
            "/api/search/download",
            json={"media_type": "tv", "tmdb_id": 12345, "title": "BB", "monitored_seasons": [1, 2]},
        )
        assert resp.status_code == 200
        sonarr.add_series_with_seasons.assert_called_once_with(999, "BB", [1, 2])

    @patch("mediaman.web.routes.search.download.build_sonarr_from_db")
    def test_tv_empty_search_seasons_rejected(self, mock_build_sonarr, authed_client):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999}
        sonarr.get_series.return_value = []
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post(
            "/api/search/download",
            json={
                "media_type": "tv",
                "tmdb_id": 12345,
                "title": "BB",
                "monitored_seasons": [1],
                "search_seasons": [],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False
        assert "season" in resp.json()["error"].lower()

    @patch("mediaman.web.routes.search.download.build_sonarr_from_db")
    def test_tv_already_tracked_blocked(self, mock_build_sonarr, authed_client):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999, "title": "BB"}
        sonarr.get_series.return_value = [{"tvdbId": 999, "title": "BB"}]
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post(
            "/api/search/download",
            json={
                "media_type": "tv",
                "tmdb_id": 12345,
                "title": "BB",
            },
        )
        assert resp.json()["ok"] is False
        assert resp.json()["error"] == "'BB' is already tracked by Sonarr — manage it there"
        sonarr.add_series.assert_not_called()


class TestSearchQueryCap:
    """/api/search must silently truncate queries over 100 chars."""

    def test_query_over_100_chars_is_accepted_and_truncated(
        self, authed_client, fake_http, fake_response
    ):
        page1 = {
            "results": [
                {
                    "media_type": "movie",
                    "id": 1,
                    "title": "Dune",
                    "poster_path": "/d.jpg",
                    "release_date": "2021-10-01",
                    "vote_average": 8.0,
                    "popularity": 100.0,
                },
            ]
        }

        def handler(method, url, **kwargs):
            page = kwargs.get("params", {}).get("page", 1)
            return fake_response(json_data=page1 if page == 1 else {"results": []})

        fake_http.handler(handler)
        long_query = "a" * 200
        resp = authed_client.get(f"/api/search?q={long_query}")
        assert resp.status_code == 200
        # The TMDB call must have received only 100 chars
        search_calls = [c for c in fake_http.calls if "search/multi" in c[1]]
        assert len(search_calls) > 0
        sent_query = search_calls[0][2]["params"]["query"]
        assert len(sent_query) <= 100


class TestDownloadNotifiesRequestingAdmin:
    """Download notification must go to the requesting admin, not first subscriber."""

    @pytest.fixture(autouse=True)
    def _clear_limiters(self):
        from mediaman.web.routes.search import (
            _DOWNLOAD_ADMIN_LIMITER,
            _DOWNLOAD_IP_LIMITER,
            _download_dedup,
        )

        _DOWNLOAD_ADMIN_LIMITER.reset()
        _DOWNLOAD_IP_LIMITER.reset()
        _download_dedup.clear()

    @patch("mediaman.web.routes.search.download.build_radarr_from_db")
    @patch("mediaman.web.routes.search.download._record_dn")
    def test_movie_download_notifies_requesting_admin(
        self, mock_record, mock_build_radarr, authed_client, app
    ):
        """The notification email must be the admin who made the request, not a subscriber."""
        from mediaman.web.auth.password_hash import set_user_email

        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = None
        radarr.add_movie.return_value = {"id": 1}
        mock_build_radarr.return_value = radarr

        conn = app.state.db
        # Set a real email on the admin so the notification row is recorded.
        set_user_email(conn, "admin", "admin@example.com")
        # Insert a subscriber with a different email to prove it's ignored.
        insert_subscriber(conn, email="other@example.com")

        authed_client.post(
            "/api/search/download",
            json={
                "media_type": "movie",
                "tmdb_id": 438631,
                "title": "Dune",
            },
        )

        assert mock_record.called
        # The email must be the admin's real address, not the subscriber's.
        call_email = mock_record.call_args[1]["email"]
        assert call_email == "admin@example.com"


class TestDiscoverSharedWithWatch:
    """Watch's discover rows and the search shelves share disk-cache keys, and the cache
    re-raises a stored failure to the next caller of the key."""

    @pytest.fixture(autouse=True)
    def _disk_cache(self, discover_disk_cache):
        return discover_disk_cache

    def test_a_watch_row_that_failed_leaves_the_search_shelf_empty(self):
        from types import SimpleNamespace

        from mediaman.services import watch_catalog
        from mediaman.web.routes.search._enrichment import _fetch_discover_shelf

        client = SimpleNamespace(
            token="t",
            trending=lambda page: [],
            popular_movies=lambda page: [{"id": 1}],
            popular_tv=lambda page: [{"id": 2}],
        )
        watch_catalog.discover_shelves(client)

        assert _fetch_discover_shelf("trending", lambda page: [{"id": 9}], None, 1) == []

    def test_an_empty_search_shelf_leaves_the_watch_row_out_not_failed(self):
        from types import SimpleNamespace

        from mediaman.services import watch_catalog
        from mediaman.web.routes.search._enrichment import _fetch_discover_shelf

        assert _fetch_discover_shelf("trending", lambda page: [], None, 1) == []
        client = SimpleNamespace(
            token="t",
            trending=lambda page: [{"id": 9}],
            popular_movies=lambda page: [{"id": 1}],
            popular_tv=lambda page: [{"id": 2}],
        )

        rows = watch_catalog.discover_shelves(client)

        assert [r[0] for r in rows] == ["popular_movies", "popular_tv"]


class TestHungTmdb:
    """A TMDB that gives no answer holds few worker threads, each once and briefly."""

    @pytest.mark.parametrize("path", ["/api/search?q=dune", "/api/search/detail/movie/5"])
    def test_one_more_than_may_wait_is_told_to_try_again(self, authed_client, monkeypatch, path):
        monkeypatch.setattr(gate, "_free", 0)

        assert authed_client.get(path).status_code == 429

    @pytest.mark.parametrize(
        "path", ["/api/search?q=dune", "/api/search/detail/movie/5", "/api/search/discover"]
    )
    def test_the_wait_is_short_and_not_repeated(
        self, authed_client, monkeypatch, fake_http, fake_response, path
    ):
        monkeypatch.setattr("time.sleep", lambda s: pytest.fail("waited to try again"))
        fake_http.default(fake_response(status=503))

        authed_client.get(path)

        assert {sum(sent["timeout"]) for _, _, sent in fake_http.calls} == {6.0}
        assert len(fake_http.calls) == len({str((url, sent)) for _, url, sent in fake_http.calls})

    def test_while_enough_wait_the_shelves_come_from_the_cache_alone(
        self, authed_client, discover_disk_cache, monkeypatch, fake_http, fake_response
    ):
        film = {"id": 1, "media_type": "movie", "title": "Dune"}
        fake_http.default(fake_response(json_data={"results": [film]}))
        authed_client.get("/api/search/discover")
        fake_http.calls.clear()
        monkeypatch.setattr(gate, "_free", 0)

        body = authed_client.get("/api/search/discover").json()

        assert (bool(body["trending"]), fake_http.calls) == (True, [])
        shutil.rmtree(disk_cache._root / "meta")
        assert authed_client.get("/api/search/discover").json()["trending"] == []
        assert fake_http.calls == []
