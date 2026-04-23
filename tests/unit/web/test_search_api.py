"""Tests for the Search page backend."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.main import create_app


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
    from mediaman.auth.session import create_session, create_user
    create_user(app.state.db, "admin", "password123", enforce_policy=False)
    token = create_session(app.state.db, "admin")
    client = TestClient(app)
    client.cookies.set("session_token", token)
    return client


class TestSearchEndpoint:
    def test_returns_merged_pages_filtered(self, authed_client, fake_http, fake_response):
        page1 = {
            "results": [
                {"media_type": "movie", "id": 1, "title": "Dune",
                 "poster_path": "/d.jpg", "release_date": "2021-10-01",
                 "vote_average": 8.0, "popularity": 100.0},
                {"media_type": "person", "id": 2, "name": "Ignored"},
            ],
        }
        page2 = {
            "results": [
                {"media_type": "tv", "id": 10, "name": "Dune: Prophecy",
                 "poster_path": "/dp.jpg", "first_air_date": "2024-11-01",
                 "vote_average": 7.5, "popularity": 80.0},
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
        assert search_calls[0][2]["params"]["include_adult"] is False

    def test_survives_single_page_failure(self, authed_client, fake_http, fake_response):
        page1 = {
            "results": [
                {"media_type": "movie", "id": 1, "title": "Dune",
                 "poster_path": "/d.jpg", "release_date": "2021-10-01",
                 "vote_average": 8.0, "popularity": 100.0},
            ],
        }

        def handler(method, url, **kwargs):
            page = kwargs.get("params", {}).get("page", 1)
            if "search/multi" in url and page == 2:
                raise RuntimeError("page 2 timeout")
            return fake_response(json_data=page1)

        fake_http.handler(handler)
        resp = authed_client.get("/api/search?q=dune")
        assert resp.status_code == 200
        data = resp.json()
        assert [r["title"] for r in data["results"]] == ["Dune"]

    def test_both_pages_failing_returns_502(self, authed_client, fake_http):
        def handler(method, url, **kwargs):
            raise Exception("down")
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
    def _clear_discover_cache(self):
        from mediaman.web.routes.search import _discover_cache
        _discover_cache.clear()
        yield
        _discover_cache.clear()

    def test_returns_three_shelves(self, authed_client, fake_http, fake_response):
        trending_payload = {
            "results": [
                {"media_type": "movie", "id": 1, "title": "Trending Movie",
                 "poster_path": "/t.jpg", "release_date": "2024-01-01",
                 "vote_average": 8.0, "popularity": 500.0},
                {"media_type": "tv", "id": 2, "name": "Trending Show",
                 "poster_path": "/ts.jpg", "first_air_date": "2023-05-01",
                 "vote_average": 7.8, "popularity": 300.0},
            ],
        }
        movies_payload = {
            "results": [
                {"id": 10, "title": "Popular Movie",
                 "poster_path": "/m.jpg", "release_date": "2024-02-01",
                 "vote_average": 7.5, "popularity": 200.0},
            ],
        }
        tv_payload = {
            "results": [
                {"id": 20, "name": "Popular Show",
                 "poster_path": "/s.jpg", "first_air_date": "2024-03-01",
                 "vote_average": 8.2, "popularity": 250.0},
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
                {"media_type": "movie", "id": 1, "title": "Good",
                 "poster_path": "/g.jpg", "release_date": "2024-01-01",
                 "vote_average": 8.0, "popularity": 100.0},
            ],
        }

        def handler(method, url, **kwargs):
            if "/tv/popular" in url:
                raise RuntimeError("sonar down")
            if kwargs.get("params", {}).get("page", 1) != 1:
                return fake_response(json_data={"results": []})
            return fake_response(json_data=good)

        fake_http.handler(handler)
        resp = authed_client.get("/api/search/discover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["popular_tv"] == []
        assert data["trending"] and data["popular_movies"]

    def test_caps_trending_at_21_and_filters_person(self, authed_client, fake_http, fake_response):
        trending_payload = {
            "results": (
                [
                    {"media_type": "movie", "id": i, "title": f"M{i}",
                     "poster_path": "/p.jpg", "release_date": "2024-01-01",
                     "vote_average": 8.0, "popularity": 100.0}
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
        from mediaman.auth.session import create_session, create_user
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


def _tmdb_movie_payload():
    return {
        "id": 438631, "title": "Dune",
        "release_date": "2021-10-22",
        "tagline": "Fear is the mind-killer.",
        "overview": "A noble family...",
        "poster_path": "/p.jpg", "backdrop_path": "/b.jpg",
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
        "id": 12345, "name": "Arcane",
        "first_air_date": "2021-11-06",
        "tagline": "Welcome to the Playground.",
        "overview": "Amid the stark discord...",
        "poster_path": "/a.jpg", "backdrop_path": "/ab.jpg",
        "episode_run_time": [42],
        "genres": [{"name": "Animation"}],
        "vote_average": 8.8,
        "created_by": [{"name": "Christian Linke"}],
        "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "vid123"}]},
        "credits": {"cast": [{"name": "Hailee Steinfeld", "character": "Vi", "profile_path": "/h.jpg"}]},
        "seasons": [
            {"season_number": 0, "name": "Specials", "episode_count": 5, "air_date": "2021-11-01"},
            {"season_number": 1, "name": "Season 1", "episode_count": 9, "air_date": "2021-11-06"},
            {"season_number": 2, "name": "Season 2", "episode_count": 9, "air_date": "2024-11-09"},
        ],
    }


class TestDetailEndpoint:
    @patch("mediaman.web.routes.search.fetch_ratings")
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

    @patch("mediaman.web.routes.search.fetch_ratings")
    def test_tv_detail_filters_season_zero(self, mock_ratings, authed_client, fake_http, fake_response):
        fake_http.default(fake_response(json_data=_tmdb_tv_payload()))
        mock_ratings.return_value = {}
        resp = authed_client.get("/api/search/detail/tv/12345")
        assert resp.status_code == 200
        data = resp.json()
        assert data["runtime"] == 42
        season_nums = [s["season_number"] for s in data["seasons"]]
        assert season_nums == [1, 2]
        assert data["sonarr_tracked"] is False

    @patch("mediaman.web.routes.search.fetch_ratings")
    def test_tv_detail_marks_in_library_seasons(self, mock_ratings, authed_client, fake_http, fake_response):
        fake_http.default(fake_response(json_data=_tmdb_tv_payload()))
        mock_ratings.return_value = {}
        with patch("mediaman.web.routes.search._fetch_sonarr_series_detail") as mock_cache:
            mock_cache.return_value = {"tracked": True, "seasons_in_library": {1}}
            resp = authed_client.get("/api/search/detail/tv/12345")
        data = resp.json()
        assert data["sonarr_tracked"] is True
        by_num = {s["season_number"]: s["in_library"] for s in data["seasons"]}
        assert by_num == {1: True, 2: False}

    def test_rejects_invalid_media_type(self, authed_client):
        resp = authed_client.get("/api/search/detail/foo/1")
        assert resp.status_code == 400

    @patch("mediaman.web.routes.search.fetch_ratings")
    def test_movie_detail_handles_missing_director_name(self, mock_ratings, authed_client, fake_http, fake_response):
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
        assert resp.headers["location"] == "/login"


class TestDownloadEndpoint:
    @patch("mediaman.web.routes.search.build_radarr_from_db")
    def test_movie_adds_to_radarr(self, mock_build_radarr, authed_client):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = None
        radarr.add_movie.return_value = {"id": 1, "title": "Dune"}
        mock_build_radarr.return_value = radarr
        resp = authed_client.post("/api/search/download", json={
            "media_type": "movie", "tmdb_id": 438631, "title": "Dune",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        radarr.add_movie.assert_called_once_with(438631, "Dune")

    @patch("mediaman.web.routes.search.build_radarr_from_db")
    def test_movie_already_in_radarr_blocked(self, mock_build_radarr, authed_client):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 1, "tmdbId": 438631}
        mock_build_radarr.return_value = radarr
        resp = authed_client.post("/api/search/download", json={
            "media_type": "movie", "tmdb_id": 438631, "title": "Dune",
        })
        assert resp.status_code == 409
        assert resp.json()["ok"] is False
        assert "already" in resp.json()["error"].lower()
        radarr.add_movie.assert_not_called()

    @patch("mediaman.web.routes.search.build_sonarr_from_db")
    def test_tv_all_seasons_flow(self, mock_build_sonarr, authed_client):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999, "title": "BB"}
        sonarr.get_series.return_value = []
        sonarr.add_series.return_value = {"id": 42}
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post("/api/search/download", json={
            "media_type": "tv", "tmdb_id": 12345, "title": "BB",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        sonarr.add_series.assert_called_once_with(999, "BB")
        sonarr.add_series_with_seasons.assert_not_called()

    @patch("mediaman.web.routes.search.build_sonarr_from_db")
    def test_tv_selective_seasons_flow(self, mock_build_sonarr, authed_client):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999, "title": "BB"}
        sonarr.get_series.return_value = []
        sonarr.add_series_with_seasons.return_value = {"id": 42}
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post("/api/search/download", json={
            "media_type": "tv", "tmdb_id": 12345, "title": "BB",
            "monitored_seasons": [1, 2, 3], "search_seasons": [2, 3],
        })
        assert resp.status_code == 200
        sonarr.add_series_with_seasons.assert_called_once_with(
            999, "BB", [1, 2, 3], [2, 3],
        )

    @patch("mediaman.web.routes.search.build_sonarr_from_db")
    def test_tv_empty_search_seasons_rejected(self, mock_build_sonarr, authed_client):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999}
        sonarr.get_series.return_value = []
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post("/api/search/download", json={
            "media_type": "tv", "tmdb_id": 12345, "title": "BB",
            "monitored_seasons": [1], "search_seasons": [],
        })
        assert resp.status_code == 400
        assert resp.json()["ok"] is False
        assert "season" in resp.json()["error"].lower()

    @patch("mediaman.web.routes.search.build_sonarr_from_db")
    def test_tv_already_tracked_blocked(self, mock_build_sonarr, authed_client):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999, "title": "BB"}
        sonarr.get_series.return_value = [{"tvdbId": 999, "title": "BB"}]
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post("/api/search/download", json={
            "media_type": "tv", "tmdb_id": 12345, "title": "BB",
        })
        assert resp.json()["ok"] is False
        assert "already tracked" in resp.json()["error"].lower()
        sonarr.add_series.assert_not_called()


class TestSearchQueryCap:
    """H12 — /api/search must silently truncate queries over 100 chars."""

    def test_query_over_100_chars_is_accepted_and_truncated(self, authed_client, fake_http, fake_response):
        """A query over 100 chars must be truncated to 100 before passing to TMDB."""
        page1 = {"results": [
            {"media_type": "movie", "id": 1, "title": "Dune",
             "poster_path": "/d.jpg", "release_date": "2021-10-01",
             "vote_average": 8.0, "popularity": 100.0},
        ]}

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
    """H24 — download notification must go to the requesting admin, not first subscriber."""

    @patch("mediaman.web.routes.search.build_radarr_from_db")
    @patch("mediaman.web.routes.search._record_dn")
    def test_movie_download_notifies_requesting_admin(self, mock_record, mock_build_radarr, authed_client, app):
        """The notification email must be the admin who made the request, not a subscriber."""
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = None
        radarr.add_movie.return_value = {"id": 1}
        mock_build_radarr.return_value = radarr

        # Insert a subscriber with a different email to prove it's ignored
        conn = app.state.db
        conn.execute(
            "INSERT INTO subscribers (email, active, created_at) "
            "VALUES ('other@example.com', 1, '2026-01-01')"
        )
        conn.commit()

        authed_client.post("/api/search/download", json={
            "media_type": "movie", "tmdb_id": 438631, "title": "Dune",
        })

        assert mock_record.called
        # The email argument should be the admin username, not 'other@example.com'
        call_email = mock_record.call_args[1].get("email") if mock_record.call_args[1] else mock_record.call_args[0][1]
        assert call_email == "admin"
        assert call_email != "other@example.com"
