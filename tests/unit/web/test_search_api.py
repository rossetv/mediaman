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
    @patch("mediaman.web.routes.search.requests.get")
    def test_returns_merged_pages_filtered(self, mock_get, authed_client):
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

        def by_page(*_args, **kwargs):
            page = kwargs["params"]["page"]
            return MagicMock(status_code=200, json=lambda p=page: page1 if p == 1 else page2)

        mock_get.side_effect = by_page

        resp = authed_client.get("/api/search?q=dune")
        assert resp.status_code == 200
        data = resp.json()
        # Page 1 movie + page 2 TV; person filtered.
        titles = {r["title"] for r in data["results"]}
        assert titles == {"Dune", "Dune: Prophecy"}

        # Both pages must be requested.
        pages_requested = sorted(call.kwargs["params"]["page"] for call in mock_get.call_args_list)
        assert pages_requested == [1, 2]
        assert mock_get.call_args_list[0].kwargs["params"]["query"] == "dune"
        assert mock_get.call_args_list[0].kwargs["params"]["include_adult"] is False

    @patch("mediaman.web.routes.search.requests.get")
    def test_survives_single_page_failure(self, mock_get, authed_client):
        page1 = {
            "results": [
                {"media_type": "movie", "id": 1, "title": "Dune",
                 "poster_path": "/d.jpg", "release_date": "2021-10-01",
                 "vote_average": 8.0, "popularity": 100.0},
            ],
        }

        def flaky(*_args, **kwargs):
            if kwargs["params"]["page"] == 2:
                raise RuntimeError("page 2 timeout")
            return MagicMock(status_code=200, json=lambda: page1)

        mock_get.side_effect = flaky
        resp = authed_client.get("/api/search?q=dune")
        assert resp.status_code == 200
        data = resp.json()
        assert [r["title"] for r in data["results"]] == ["Dune"]

    @patch("mediaman.web.routes.search.requests.get")
    def test_both_pages_failing_returns_502(self, mock_get, authed_client):
        mock_get.side_effect = Exception("down")
        resp = authed_client.get("/api/search?q=dune")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_short_query_returns_empty_without_tmdb_call(self, authed_client):
        with patch("mediaman.web.routes.search.requests.get") as mock_get:
            resp = authed_client.get("/api/search?q=d")
            assert resp.status_code == 200
            assert resp.json() == {"results": []}
            mock_get.assert_not_called()

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

    @patch("mediaman.web.routes.search.requests.get")
    def test_returns_three_shelves(self, mock_get, authed_client):
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
                # Popular endpoint doesn't include media_type; server must inject.
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

        def by_url(url, **_kwargs):
            if "/trending/" in url:
                return MagicMock(status_code=200, json=lambda: trending_payload)
            if "/movie/popular" in url:
                return MagicMock(status_code=200, json=lambda: movies_payload)
            if "/tv/popular" in url:
                return MagicMock(status_code=200, json=lambda: tv_payload)
            raise AssertionError(f"unexpected url: {url}")

        mock_get.side_effect = by_url
        resp = authed_client.get("/api/search/discover")
        assert resp.status_code == 200
        data = resp.json()
        assert [r["title"] for r in data["trending"]] == ["Trending Movie", "Trending Show"]
        assert [r["title"] for r in data["popular_movies"]] == ["Popular Movie"]
        assert [r["media_type"] for r in data["popular_movies"]] == ["movie"]
        assert [r["title"] for r in data["popular_tv"]] == ["Popular Show"]
        assert [r["media_type"] for r in data["popular_tv"]] == ["tv"]

    @patch("mediaman.web.routes.search.requests.get")
    def test_survives_single_shelf_failure(self, mock_get, authed_client):
        good = {
            "results": [
                {"media_type": "movie", "id": 1, "title": "Good",
                 "poster_path": "/g.jpg", "release_date": "2024-01-01",
                 "vote_average": 8.0, "popularity": 100.0},
            ],
        }

        def selective(url, **_kwargs):
            if "/tv/popular" in url:
                raise RuntimeError("sonar down")
            return MagicMock(status_code=200, json=lambda: good)

        mock_get.side_effect = selective
        resp = authed_client.get("/api/search/discover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["popular_tv"] == []
        assert data["trending"] and data["popular_movies"]

    @patch("mediaman.web.routes.search.requests.get")
    def test_caps_trending_at_30_and_filters_person(self, mock_get, authed_client):
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

        def by_url(url, **_kwargs):
            if "/trending/" in url:
                return MagicMock(status_code=200, json=lambda: trending_payload)
            return MagicMock(status_code=200, json=lambda: {"results": []})

        mock_get.side_effect = by_url
        resp = authed_client.get("/api/search/discover")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["trending"]) == 30
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
    @patch("mediaman.services.tmdb.requests.get")
    def test_movie_detail_shape(self, mock_get, mock_ratings, authed_client):
        mock_get.return_value = MagicMock(ok=True, status_code=200, json=lambda: _tmdb_movie_payload())
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
    @patch("mediaman.services.tmdb.requests.get")
    def test_tv_detail_filters_season_zero(self, mock_get, mock_ratings, authed_client):
        mock_get.return_value = MagicMock(ok=True, status_code=200, json=lambda: _tmdb_tv_payload())
        mock_ratings.return_value = {}
        resp = authed_client.get("/api/search/detail/tv/12345")
        assert resp.status_code == 200
        data = resp.json()
        assert data["runtime"] == 42
        season_nums = [s["season_number"] for s in data["seasons"]]
        assert season_nums == [1, 2]
        assert data["sonarr_tracked"] is False

    @patch("mediaman.web.routes.search.fetch_ratings")
    @patch("mediaman.services.tmdb.requests.get")
    def test_tv_detail_marks_in_library_seasons(self, mock_get, mock_ratings, authed_client):
        mock_get.return_value = MagicMock(ok=True, status_code=200, json=lambda: _tmdb_tv_payload())
        mock_ratings.return_value = {}
        with patch("mediaman.web.routes.search._build_sonarr_detail_cache") as mock_cache:
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
    @patch("mediaman.services.tmdb.requests.get")
    def test_movie_detail_handles_missing_director_name(self, mock_get, mock_ratings, authed_client):
        payload = _tmdb_movie_payload()
        payload["credits"]["crew"] = [{"job": "Director"}]  # no 'name' key
        mock_get.return_value = MagicMock(ok=True, status_code=200, json=lambda: payload)
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
    @patch("mediaman.web.routes.search._build_radarr")
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

    @patch("mediaman.web.routes.search._build_radarr")
    def test_movie_already_in_radarr_blocked(self, mock_build_radarr, authed_client):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 1, "tmdbId": 438631}
        mock_build_radarr.return_value = radarr
        resp = authed_client.post("/api/search/download", json={
            "media_type": "movie", "tmdb_id": 438631, "title": "Dune",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "already" in resp.json()["error"].lower()
        radarr.add_movie.assert_not_called()

    @patch("mediaman.web.routes.search._build_sonarr")
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

    @patch("mediaman.web.routes.search._build_sonarr")
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

    @patch("mediaman.web.routes.search._build_sonarr")
    def test_tv_empty_search_seasons_rejected(self, mock_build_sonarr, authed_client):
        sonarr = MagicMock()
        sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999}
        sonarr.get_series.return_value = []
        mock_build_sonarr.return_value = sonarr
        resp = authed_client.post("/api/search/download", json={
            "media_type": "tv", "tmdb_id": 12345, "title": "BB",
            "monitored_seasons": [1], "search_seasons": [],
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "season" in resp.json()["error"].lower()

    @patch("mediaman.web.routes.search._build_sonarr")
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
