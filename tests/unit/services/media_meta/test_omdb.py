"""Tests for the OMDb ratings helper."""

from __future__ import annotations

import pytest

from mediaman.db import init_db
from mediaman.services.media_meta import omdb
from mediaman.services.media_meta.omdb import fetch_ratings, lookup_ratings


@pytest.fixture(autouse=True)
def _no_refused_key(monkeypatch):
    monkeypatch.setattr(omdb, "_refused", None)


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
        fake_http.queue(
            "GET",
            fake_response(
                json_data={
                    "Response": "True",
                    "imdbRating": "8.0",
                    "Metascore": "74",
                    "Ratings": [
                        {"Source": "Internet Movie Database", "Value": "8.0/10"},
                        {"Source": "Rotten Tomatoes", "Value": "83%"},
                    ],
                }
            ),
        )
        ratings = fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key)
        assert ratings == {"imdb": "8.0", "rt": "83%", "metascore": "74"}

    def test_omits_missing_values(self, conn, secret_key, fake_http, fake_response):
        _set_key(conn, "plain-key")
        fake_http.queue(
            "GET",
            fake_response(
                json_data={
                    "Response": "True",
                    "imdbRating": "N/A",
                    "Metascore": "N/A",
                    "Ratings": [],
                }
            ),
        )
        assert fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key) == {}

    def test_returns_empty_on_http_error(self, conn, secret_key, fake_http, fake_response):
        _set_key(conn, "plain-key")
        fake_http.queue("GET", fake_response(status=500, text="bad"))
        assert fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key) == {}

    def test_returns_empty_on_exception(self, conn, secret_key, fake_http):
        """A network failure (RequestException family) returns empty rather than raising.

        ``fetch_ratings`` deliberately narrows to the realistic failure
        modes ``(SafeHTTPError, requests.RequestException, ValueError,
        KeyError)`` so a generic programming bug propagates instead of
        being silently masked. ``requests.Timeout`` is a subclass of
        ``RequestException`` so a real OMDb timeout still hits the empty
        return path.
        """
        import requests

        _set_key(conn, "plain-key")
        fake_http.raise_on("GET", requests.Timeout("timeout"))
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

    @pytest.mark.parametrize(
        "ratings",
        [None, "83%", ["junk", 5, None], [{"Source": "Rotten Tomatoes"}]],
        ids=["null", "string", "non-dict entries", "no value"],
    )
    def test_a_malformed_ratings_list_keeps_what_else_the_reply_holds(
        self, conn, secret_key, fake_http, fake_response, ratings
    ):
        _set_key(conn, "plain-key")
        reply = {"Response": "True", "imdbRating": "8.0", "Metascore": 74, "Ratings": ratings}
        fake_http.queue("GET", fake_response(json_data=reply))

        assert fetch_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key) == {
            "imdb": "8.0"
        }


class TestLookupRatings:
    """None means OMDb gave no answer; an empty dict means it answered that it has none."""

    def _lookup(self, conn, secret_key):
        return lookup_ratings("Dune", 2021, "movie", conn=conn, secret_key=secret_key)

    def test_no_key_is_no_answer(self, conn, secret_key):
        assert self._lookup(conn, secret_key) is None

    def test_a_timeout_is_no_answer(self, conn, secret_key, fake_http):
        import requests

        _set_key(conn, "plain-key")
        fake_http.raise_on("GET", requests.Timeout("timeout"))
        assert self._lookup(conn, secret_key) is None

    def test_a_non_json_body_is_no_answer(self, conn, secret_key, fake_http, fake_response):
        _set_key(conn, "plain-key")
        resp = fake_response(text="not json")
        resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
        fake_http.queue("GET", resp)
        assert self._lookup(conn, secret_key) is None

    @pytest.mark.parametrize("error", ["Request limit reached!", "Invalid API key!"])
    def test_a_spent_quota_or_bad_key_is_no_answer(
        self, conn, secret_key, fake_http, fake_response, error
    ):
        _set_key(conn, "plain-key")
        fake_http.queue("GET", fake_response(json_data={"Response": "False", "Error": error}))
        assert self._lookup(conn, secret_key) is None

    def test_a_title_it_does_not_know_is_an_answer_of_no_ratings(
        self, conn, secret_key, fake_http, fake_response
    ):
        _set_key(conn, "plain-key")
        fake_http.queue(
            "GET", fake_response(json_data={"Response": "False", "Error": "Movie not found!"})
        )
        assert self._lookup(conn, secret_key) == {}


class TestRefusedKey:
    """OMDb refusing the key (a bad key, a spent quota) is not asked again with it for an hour."""

    @pytest.fixture
    def clock(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(omdb, "_clock", lambda: now[0])
        return now

    @staticmethod
    def _lookup(key="spent"):
        return lookup_ratings("Dune", 2021, "movie", omdb_key=key)

    @staticmethod
    def _good(fake_response):
        return fake_response(json_data={"Response": "True", "imdbRating": "8.0"})

    @pytest.mark.parametrize(
        "reply",
        [
            {"status": 401, "text": "no"},
            {"json_data": {"Response": "False", "Error": "Request limit reached!"}},
            {"json_data": {"Response": "False", "Error": "Invalid API key!"}},
        ],
        ids=["401", "quota", "bad key"],
    )
    def test_a_refused_key_is_asked_again_only_after_an_hour(
        self, fake_http, fake_response, clock, reply
    ):
        fake_http.queue("GET", fake_response(**reply))
        fake_http.queue("GET", self._good(fake_response))
        assert self._lookup() is None

        clock[0] += 3599
        assert omdb.is_key_refused("spent")
        assert self._lookup() is None
        assert len(fake_http.calls) == 1

        clock[0] += 1
        assert self._lookup() == {"imdb": "8.0"}

    def test_a_new_key_is_asked_at_once(self, fake_http, fake_response, clock):
        fake_http.queue("GET", fake_response(status=401, text="no"))
        fake_http.queue("GET", self._good(fake_response))
        self._lookup()

        assert self._lookup("new") == {"imdb": "8.0"}

    def test_an_error_about_one_title_does_not_refuse_the_key(
        self, fake_http, fake_response, clock
    ):
        fake_http.queue(
            "GET", fake_response(json_data={"Response": "False", "Error": "Too many results."})
        )

        assert self._lookup() is None
        assert not omdb.is_key_refused("spent")
