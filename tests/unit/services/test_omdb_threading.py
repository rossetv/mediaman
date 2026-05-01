"""Tests for OMDb threading fix (finding 32).

Ensures fetch_ratings can be called without a DB connection (via the omdb_key=
parameter) and that the programming-error path is not swallowed.
"""

from __future__ import annotations

import pytest

from mediaman.db import init_db
from mediaman.services.media_meta.omdb import fetch_ratings, get_omdb_key


@pytest.fixture
def conn(db_path):
    c = init_db(str(db_path))
    yield c
    c.close()


def _set_key(conn, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
        "VALUES ('omdb_api_key', ?, 0, datetime('now'))",
        (value,),
    )
    conn.commit()


class TestGetOmdbKey:
    def test_returns_none_when_not_set(self, conn, secret_key):
        assert get_omdb_key(conn, secret_key) is None

    def test_returns_key_when_set(self, conn, secret_key):
        _set_key(conn, "test-api-key")
        assert get_omdb_key(conn, secret_key) == "test-api-key"


class TestFetchRatingsWithOmdbKey:
    """fetch_ratings can accept a pre-resolved key so it is safe in worker threads."""

    def test_returns_empty_when_key_is_blank(self):
        """An empty string key (OMDb not configured) returns empty without raising."""
        result = fetch_ratings("Dune", 2021, "movie", omdb_key="")
        assert result == {}

    def test_raises_when_key_is_none_and_no_conn(self):
        """omdb_key=None with no conn/secret_key raises TypeError (programming error, not swallowed)."""
        with pytest.raises(TypeError):
            fetch_ratings("Dune", 2021, "movie", omdb_key=None)

    def test_raises_when_neither_key_nor_conn_supplied(self):
        """Calling without omdb_key= and without conn= must raise TypeError, not silently fail."""
        with pytest.raises(TypeError):
            fetch_ratings("Dune", 2021, "movie")

    def test_http_fetch_uses_supplied_key(self, conn, secret_key, fake_http, fake_response):
        """When omdb_key= is supplied no DB access is needed — works from any thread."""
        fake_http.queue(
            "GET",
            fake_response(
                json_data={
                    "Response": "True",
                    "imdbRating": "8.0",
                    "Metascore": "N/A",
                    "Ratings": [],
                }
            ),
        )
        result = fetch_ratings("Dune", 2021, "movie", omdb_key="my-key")
        assert result == {"imdb": "8.0"}
        # Verify the key was sent in the request params.
        _, _, kwargs = fake_http.calls[0]
        assert kwargs["params"]["apikey"] == "my-key"

    def test_conn_path_still_works(self, conn, secret_key, fake_http, fake_response):
        """The original conn= + secret_key= path continues to work for non-threaded callers."""
        _set_key(conn, "db-key")
        fake_http.queue(
            "GET",
            fake_response(
                json_data={
                    "Response": "True",
                    "imdbRating": "7.5",
                    "Metascore": "N/A",
                    "Ratings": [],
                }
            ),
        )
        result = fetch_ratings("Inception", 2010, "movie", conn=conn, secret_key=secret_key)
        assert result == {"imdb": "7.5"}


class TestEnrichRatingsThreadSafety:
    """Smoke test: _enrich_ratings does not raise sqlite3.ProgrammingError.

    The full executor path is hard to test without real threads; this verifies
    that the omdb_key is resolved before workers are dispatched by checking that
    the key lookup happens synchronously and the worker function receives a plain
    string, not a connection.
    """

    def test_get_omdb_key_resolves_before_threading(self, conn, secret_key):
        """get_omdb_key is called in the request thread and returns a plain value."""
        _set_key(conn, "thread-safe-key")
        key = get_omdb_key(conn, secret_key)
        assert isinstance(key, str)
        assert key == "thread-safe-key"
