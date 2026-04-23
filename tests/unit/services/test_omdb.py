"""Tests for the OMDb ratings helper."""
import pytest

from mediaman.db import init_db
from mediaman.services.omdb import fetch_ratings


@pytest.fixture
def conn(db_path):
    c = init_db(str(db_path))
    yield c
    c.close()


def _set_key(conn, value, encrypted=0):
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) VALUES ('omdb_api_key', ?, ?, datetime('now'))",
        (value, encrypted),
    )
    conn.commit()


class TestFetchRatings:
    def test_returns_empty_when_key_missing(self, conn, secret_key):
        assert fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key) == {}

    def test_returns_empty_when_key_blank(self, conn, secret_key):
        _set_key(conn, "")
        assert fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key) == {}

    def test_parses_known_ratings(self, conn, secret_key, fake_http, fake_response):
        _set_key(conn, "plain-key")
        fake_http.queue("GET", fake_response(json_data={
            "Response": "True",
            "imdbRating": "8.0",
            "Metascore": "74",
            "Ratings": [
                {"Source": "Internet Movie Database", "Value": "8.0/10"},
                {"Source": "Rotten Tomatoes", "Value": "83%"},
            ],
        }))
        ratings = fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key)
        assert ratings == {"imdb": "8.0", "rt": "83%", "metascore": "74"}

    def test_omits_missing_values(self, conn, secret_key, fake_http, fake_response):
        _set_key(conn, "plain-key")
        fake_http.queue("GET", fake_response(json_data={
            "Response": "True",
            "imdbRating": "N/A",
            "Metascore": "N/A",
            "Ratings": [],
        }))
        assert fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key) == {}

    def test_returns_empty_on_http_error(self, conn, secret_key, fake_http, fake_response):
        _set_key(conn, "plain-key")
        fake_http.queue("GET", fake_response(status=500, text="bad"))
        assert fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key) == {}

    def test_returns_empty_on_exception(self, conn, secret_key, fake_http):
        _set_key(conn, "plain-key")
        fake_http.raise_on("GET", Exception("timeout"))
        assert fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key) == {}

    def test_sends_series_type_for_tv(self, conn, secret_key, fake_http, fake_response):
        _set_key(conn, "plain-key")
        fake_http.queue("GET", fake_response(json_data={"Response": "True", "Ratings": []}))
        fetch_ratings("Arcane", 2021, "tv", conn=conn, secret_key=secret_key)
        _, _, kwargs = fake_http.calls[0]
        params = kwargs["params"]
        assert params["type"] == "series"
        assert params["t"] == "Arcane"
        assert params["y"] == 2021

    def test_returns_empty_on_non_json_body(self, conn, secret_key, fake_http, fake_response):
        _set_key(conn, "plain-key")
        resp = fake_response(text="not json")
        resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
        fake_http.queue("GET", resp)
        assert fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key) == {}

    def test_returns_empty_on_non_dict_body(self, conn, secret_key, fake_http, fake_response):
        _set_key(conn, "plain-key")
        fake_http.queue("GET", fake_response(json_data=["not", "a", "dict"]))
        assert fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key) == {}
