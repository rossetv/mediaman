"""Tests for the unified TMDB client and shaping helpers."""

from __future__ import annotations

import pytest
import requests

from mediaman.crypto import encrypt_value
from mediaman.db import init_db
from mediaman.services.media_meta.tmdb import TmdbClient


@pytest.fixture
def conn(db_path):
    c = init_db(str(db_path))
    yield c
    c.close()


def _set_token(conn, value, encrypted=0):
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
        "VALUES ('tmdb_read_token', ?, ?, datetime('now'))",
        (value, encrypted),
    )
    conn.commit()


def _movie_search_payload():
    return {
        "results": [
            {
                "id": 438631,
                "title": "Dune",
                "release_date": "2021-10-22",
                "overview": "A noble family...",
                "poster_path": "/p.jpg",
                "vote_average": 8.012,
            }
        ]
    }


def _movie_details_payload():
    return {
        "id": 438631,
        "title": "Dune",
        "release_date": "2021-10-22",
        "tagline": "Fear is the mind-killer.",
        "overview": "A noble family...",
        "poster_path": "/p.jpg",
        "runtime": 155,
        "genres": [{"name": "Sci-Fi"}, {"name": "Adventure"}],
        "vote_average": 8.0,
        "videos": {
            "results": [
                {"site": "YouTube", "type": "Teaser", "key": "teaser1"},
                {"site": "YouTube", "type": "Trailer", "key": "abc123"},
            ]
        },
        "credits": {
            "crew": [
                {"job": "Writer", "name": "Unknown"},
                {"job": "Director", "name": "Denis Villeneuve"},
            ],
            "cast": [{"name": f"Actor {i}", "character": f"Role {i}"} for i in range(10)],
        },
    }


def _tv_details_payload():
    return {
        "id": 12345,
        "name": "Arcane",
        "first_air_date": "2021-11-06",
        "tagline": "Welcome to the Playground.",
        "overview": "Amid the stark discord...",
        "poster_path": "/a.jpg",
        "episode_run_time": [42, 45],
        "genres": [{"name": "Animation"}],
        "vote_average": 8.8,
        "created_by": [{"name": "Christian Linke"}, {"name": "Alex Yee"}],
        "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "vid123"}]},
        "credits": {
            "cast": [{"name": "Hailee Steinfeld", "character": "Vi"}],
        },
    }


class TestFromDb:
    def test_returns_none_when_token_missing(self, conn, secret_key):
        assert TmdbClient.from_db(conn, secret_key) is None

    def test_returns_none_when_token_blank(self, conn, secret_key):
        _set_token(conn, "")
        assert TmdbClient.from_db(conn, secret_key) is None

    def test_builds_client_with_plain_token(self, conn, secret_key):
        _set_token(conn, "plain-token")
        client = TmdbClient.from_db(conn, secret_key)
        assert client is not None
        assert client._headers["Authorization"] == "Bearer plain-token"

    def test_decrypts_encrypted_token(self, conn, secret_key):
        ct = encrypt_value("real-token", secret_key, conn=conn, aad=b"tmdb_read_token")
        _set_token(conn, ct, encrypted=1)
        client = TmdbClient.from_db(conn, secret_key)
        assert client is not None
        assert client._headers["Authorization"] == "Bearer real-token"

    def test_returns_none_on_decrypt_failure(self, conn, secret_key):
        _set_token(conn, "garbage-that-cant-be-decrypted", encrypted=1)
        assert TmdbClient.from_db(conn, secret_key) is None


class TestSearch:
    def test_movie_search_returns_first_result(self, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=_movie_search_payload()))
        client = TmdbClient("token")
        result = client.search("Dune")
        assert result["id"] == 438631
        _, url, kwargs = fake_http.calls[0]
        assert kwargs["headers"]["Authorization"] == "Bearer token"
        assert url.endswith("/search/movie")
        params = kwargs["params"]
        assert params["query"] == "Dune"
        assert "year" not in params

    def test_movie_search_with_year(self, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=_movie_search_payload()))
        client = TmdbClient("token")
        client.search("Dune", year=2021, media_type="movie")
        assert fake_http.calls[0][2]["params"]["year"] == 2021

    def test_tv_search_uses_first_air_date_year(self, fake_http, fake_response):
        fake_http.queue(
            "GET",
            fake_response(
                json_data={"results": [{"id": 1, "name": "X", "first_air_date": "2021-01-01"}]}
            ),
        )
        client = TmdbClient("token")
        client.search("Arcane", year=2021, media_type="tv")
        _, url, kwargs = fake_http.calls[0]
        assert url.endswith("/search/tv")
        assert kwargs["params"]["first_air_date_year"] == 2021

    def test_returns_none_on_empty_results(self, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"results": []}))
        client = TmdbClient("token")
        assert client.search("Nothing") is None

    def test_returns_none_on_http_error(self, fake_http, fake_response):
        fake_http.queue("GET", fake_response(status=500, text="no"))
        client = TmdbClient("token")
        assert client.search("Dune") is None

    def test_returns_none_on_exception(self, fake_http):
        fake_http.raise_on("GET", requests.ConnectionError("timeout"))
        client = TmdbClient("token")
        assert client.search("Dune") is None


class TestSearchMulti:
    def test_returns_raw_first_result(self, fake_http, fake_response):
        fake_http.queue(
            "GET",
            fake_response(
                json_data={
                    "results": [
                        {"media_type": "person", "id": 999},
                        {"media_type": "movie", "id": 1, "poster_path": "/x.jpg"},
                    ]
                }
            ),
        )
        client = TmdbClient("token")
        result = client.search_multi("Anything")
        assert result["id"] == 999

    def test_returns_none_on_failure(self, fake_http):
        fake_http.raise_on("GET", requests.ConnectionError("boom"))
        client = TmdbClient("token")
        assert client.search_multi("x") is None


class TestDetails:
    def test_movie_details_appends_videos_and_credits(self, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=_movie_details_payload()))
        client = TmdbClient("token")
        data = client.details("movie", 438631)
        assert data["id"] == 438631
        _, url, kwargs = fake_http.calls[0]
        assert url.endswith("/movie/438631")
        assert kwargs["params"]["append_to_response"] == "videos,credits"

    def test_tv_details_uses_tv_endpoint(self, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=_tv_details_payload()))
        client = TmdbClient("token")
        client.details("tv", 12345)
        assert fake_http.calls[0][1].endswith("/tv/12345")

    def test_returns_none_on_http_error(self, fake_http, fake_response):
        fake_http.queue("GET", fake_response(status=500))
        client = TmdbClient("token")
        assert client.details("movie", 1) is None

    def test_returns_none_on_exception(self, fake_http):
        fake_http.raise_on("GET", requests.ConnectionError("down"))
        client = TmdbClient("token")
        assert client.details("movie", 1) is None


class TestShapeCard:
    def test_movie_payload(self):
        card = TmdbClient.shape_card(_movie_search_payload()["results"][0])
        assert card["tmdb_id"] == 438631
        assert card["year"] == 2021
        assert card["poster_url"] == "https://image.tmdb.org/t/p/w300/p.jpg"
        # vote_average 8.012 rounded to 1dp
        assert card["rating"] == 8.0
        assert card["description"] == "A noble family..."

    def test_tv_payload_uses_first_air_date(self):
        item = {
            "id": 5,
            "name": "X",
            "first_air_date": "2024-05-05",
            "overview": "O",
            "poster_path": None,
            "vote_average": 0,  # falsy — should be None
        }
        card = TmdbClient.shape_card(item)
        assert card["year"] == 2024
        assert card["poster_url"] is None
        assert card["rating"] is None

    def test_missing_fields_return_safe_defaults(self):
        card = TmdbClient.shape_card({"id": 1})
        assert card["year"] is None
        assert card["poster_url"] is None
        assert card["rating"] is None
        assert card["description"] == ""

    def test_malformed_year_is_none(self):
        # release_date may be an empty string or malformed — don't crash
        card = TmdbClient.shape_card({"id": 1, "release_date": ""})
        assert card["year"] is None

    def test_rating_rounded_to_1dp(self):
        card = TmdbClient.shape_card({"id": 1, "vote_average": 7.456})
        assert card["rating"] == 7.5


class TestShapeDetail:
    def test_movie_details(self):
        out = TmdbClient.shape_detail(_movie_details_payload(), media_type="movie")
        assert out["tagline"] == "Fear is the mind-killer."
        assert out["runtime"] == 155
        # genres serialised as JSON string — matches suggestions table format
        assert out["genres"] == '["Sci-Fi", "Adventure"]'
        assert out["director"] == "Denis Villeneuve"
        # Cast truncated to top 8 with name + character only
        import json

        cast = json.loads(out["cast_json"])
        assert len(cast) == 8
        assert cast[0] == {"name": "Actor 0", "character": "Role 0"}
        # First Trailer wins even though a Teaser appeared first
        assert out["trailer_key"] == "abc123"

    def test_tv_details_picks_first_runtime_and_creator(self):
        out = TmdbClient.shape_detail(_tv_details_payload(), media_type="tv")
        # episode_run_time is [42, 45] — first value used
        assert out["runtime"] == 42
        # First creator used for director field
        assert out["director"] == "Christian Linke"
        assert out["trailer_key"] == "vid123"

    def test_missing_tagline_yields_none(self):
        out = TmdbClient.shape_detail({"id": 1, "tagline": ""}, media_type="movie")
        assert out["tagline"] is None

    def test_no_runtime_for_tv_when_missing(self):
        out = TmdbClient.shape_detail({"id": 1, "episode_run_time": []}, media_type="tv")
        assert out["runtime"] is None

    def test_empty_genres_returns_none(self):
        out = TmdbClient.shape_detail({"id": 1, "genres": []}, media_type="movie")
        assert out["genres"] is None

    def test_missing_director_returns_none(self):
        out = TmdbClient.shape_detail(
            {"id": 1, "credits": {"crew": [{"job": "Writer", "name": "W"}]}},
            media_type="movie",
        )
        assert out["director"] is None

    def test_empty_cast_returns_none_cast_json(self):
        out = TmdbClient.shape_detail({"id": 1, "credits": {"cast": []}}, media_type="movie")
        assert out["cast_json"] is None

    def test_trailer_skips_non_youtube(self):
        out = TmdbClient.shape_detail(
            {
                "id": 1,
                "videos": {
                    "results": [
                        {"site": "Vimeo", "type": "Trailer", "key": "vim1"},
                        {"site": "YouTube", "type": "Teaser", "key": "teaser"},
                        {"site": "YouTube", "type": "Trailer", "key": "yt"},
                    ]
                },
            },
            media_type="movie",
        )
        assert out["trailer_key"] == "yt"

    def test_missing_trailer_yields_none(self):
        out = TmdbClient.shape_detail({"id": 1}, media_type="movie")
        assert out["trailer_key"] is None
