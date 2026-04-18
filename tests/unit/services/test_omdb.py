"""Tests for the OMDb ratings helper."""
from unittest.mock import MagicMock, patch

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
        assert fetch_ratings("Dune", 2021, "movie", conn, secret_key) == {}

    def test_returns_empty_when_key_blank(self, conn, secret_key):
        _set_key(conn, "")
        assert fetch_ratings("Dune", 2021, "movie", conn, secret_key) == {}

    @patch("mediaman.services.omdb.requests.get")
    def test_parses_known_ratings(self, mock_get, conn, secret_key):
        _set_key(conn, "plain-key")
        mock_get.return_value = MagicMock(
            ok=True,
            json=lambda: {
                "Response": "True",
                "imdbRating": "8.0",
                "Metascore": "74",
                "Ratings": [
                    {"Source": "Internet Movie Database", "Value": "8.0/10"},
                    {"Source": "Rotten Tomatoes", "Value": "83%"},
                ],
            },
        )
        ratings = fetch_ratings("Dune", 2021, "movie", conn, secret_key)
        assert ratings == {"imdb": "8.0", "rt": "83%", "metascore": "74"}

    @patch("mediaman.services.omdb.requests.get")
    def test_omits_missing_values(self, mock_get, conn, secret_key):
        _set_key(conn, "plain-key")
        mock_get.return_value = MagicMock(
            ok=True,
            json=lambda: {
                "Response": "True",
                "imdbRating": "N/A",
                "Metascore": "N/A",
                "Ratings": [],
            },
        )
        assert fetch_ratings("Dune", 2021, "movie", conn, secret_key) == {}

    @patch("mediaman.services.omdb.requests.get")
    def test_returns_empty_on_http_error(self, mock_get, conn, secret_key):
        _set_key(conn, "plain-key")
        mock_get.return_value = MagicMock(ok=False)
        assert fetch_ratings("Dune", 2021, "movie", conn, secret_key) == {}

    @patch("mediaman.services.omdb.requests.get")
    def test_returns_empty_on_exception(self, mock_get, conn, secret_key):
        _set_key(conn, "plain-key")
        mock_get.side_effect = Exception("timeout")
        assert fetch_ratings("Dune", 2021, "movie", conn, secret_key) == {}

    @patch("mediaman.services.omdb.requests.get")
    def test_sends_series_type_for_tv(self, mock_get, conn, secret_key):
        _set_key(conn, "plain-key")
        mock_get.return_value = MagicMock(
            ok=True,
            json=lambda: {"Response": "True", "Ratings": []},
        )
        fetch_ratings("Arcane", 2021, "tv", conn, secret_key)
        params = mock_get.call_args[1]["params"]
        assert params["type"] == "series"
        assert params["t"] == "Arcane"
        assert params["y"] == 2021

    @patch("mediaman.services.omdb.requests.get")
    def test_returns_empty_on_non_json_body(self, mock_get, conn, secret_key):
        _set_key(conn, "plain-key")
        resp = MagicMock(ok=True)
        resp.json.side_effect = ValueError("not json")
        mock_get.return_value = resp
        assert fetch_ratings("Dune", 2021, "movie", conn, secret_key) == {}

    @patch("mediaman.services.omdb.requests.get")
    def test_returns_empty_on_non_dict_body(self, mock_get, conn, secret_key):
        _set_key(conn, "plain-key")
        mock_get.return_value = MagicMock(ok=True, json=lambda: ["not", "a", "dict"])
        assert fetch_ratings("Dune", 2021, "movie", conn, secret_key) == {}
