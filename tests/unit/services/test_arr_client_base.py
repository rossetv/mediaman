"""Tests for the shared ArrClient HTTP base class."""

import pytest

from mediaman.services.arr.base import _ArrClientBase
from mediaman.services.infra.http import SafeHTTPError


class _TestClient(_ArrClientBase):
    """Minimal concrete subclass for testing the HTTP plumbing directly.

    Uses :class:`_ArrClientBase` rather than the public spec-driven
    :class:`ArrClient` so the tests cover only the shared HTTP layer
    (``_get``/``_put``/``_post``/``_delete`` + lookup helpers) without
    needing to thread an :class:`ArrSpec` through every fixture.
    """


@pytest.fixture
def client() -> _TestClient:
    return _TestClient("http://arr.local:7878", "test-api-key")


class TestGetMethod:
    def test_get_returns_json(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"status": "ok"}))
        assert client._get("/api/v3/system/status") == {"status": "ok"}

    def test_get_raises_on_http_error(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(status=404, text="not found"))
        with pytest.raises(SafeHTTPError):
            client._get("/api/v3/movie")

    def test_get_passes_api_key_header(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=[]))
        client._get("/api/v3/movie")
        _, _, kwargs = fake_http.calls[0]
        assert kwargs["headers"]["X-Api-Key"] == "test-api-key"


class TestPutMethod:
    def test_put_sends_json_body(self, client, fake_http, fake_response):
        fake_http.queue("PUT", fake_response(status=202, content=b""))
        client._put("/api/v3/movie/1", data={"key": "val"})
        _, _, kwargs = fake_http.calls[0]
        assert kwargs["json"] == {"key": "val"}

    def test_put_includes_correct_url(self, client, fake_http, fake_response):
        fake_http.queue("PUT", fake_response(status=202, content=b""))
        client._put("/api/v3/movie/42", data={})
        _, url, _ = fake_http.calls[0]
        assert url == "http://arr.local:7878/api/v3/movie/42"


class TestDeleteMethod:
    def test_delete_sends_request(self, client, fake_http, fake_response):
        fake_http.queue("DELETE", fake_response(content=b""))
        client._delete("/api/v3/movie/1")
        assert len(fake_http.calls) == 1

    def test_delete_includes_correct_url(self, client, fake_http, fake_response):
        fake_http.queue("DELETE", fake_response(content=b""))
        client._delete("/api/v3/movie/99")
        _, url, _ = fake_http.calls[0]
        assert url == "http://arr.local:7878/api/v3/movie/99"


class TestTestConnection:
    def test_test_connection_true(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"version": "5.0"}))
        assert client.is_reachable() is True

    def test_test_connection_false_on_exception(self, client, fake_http):
        import requests

        fake_http.raise_on("GET", requests.ConnectionError("unreachable"))
        assert client.is_reachable() is False

    def test_test_connection_false_on_http_error(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(status=401, text="nope"))
        assert client.is_reachable() is False


class TestConstructor:
    def test_trailing_slash_stripped_from_url(self):
        c = _TestClient("http://arr.local/", "key")
        assert not c._url.endswith("/")

    def test_api_key_stored_in_header(self):
        """API key is forwarded verbatim in the X-Api-Key header."""
        c = _TestClient("http://arr.local", "my-secret-key")
        assert c._headers["X-Api-Key"] == "my-secret-key"


class TestLastError:
    """H35 -- last_error is populated on failure and cleared on success."""

    def test_last_error_initially_none(self):
        """last_error is None before any call is made."""
        from mediaman.services.arr.base import _ArrClientBase

        class TC(_ArrClientBase):
            pass

        c = TC("http://arr.local", "key")
        assert c.last_error is None

    def test_split_timeout_applied(self):
        """SafeHTTPClient is configured with the split connect/read timeout."""
        from mediaman.services.arr.base import _ARR_TIMEOUT_SECONDS, _ArrClientBase

        class TC(_ArrClientBase):
            pass

        c = TC("http://arr.local", "key")
        assert c._http._default_timeout == _ARR_TIMEOUT_SECONDS
        assert _ARR_TIMEOUT_SECONDS == (5.0, 30.0)

    def test_last_error_cleared_on_success(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"status": "ok"}))
        client._get("/api/v3/system/status")
        assert client.last_error is None

    def test_last_error_set_on_get_failure(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(status=500, text="boom"))
        with pytest.raises(Exception):
            client._get("/api/v3/system/status")
        assert client.last_error is not None
        assert len(client.last_error) > 0

    def test_last_error_set_on_post_failure(self, client, fake_http, fake_response):
        fake_http.queue("POST", fake_response(status=503, text="unavailable"))
        with pytest.raises(Exception):
            client._post("/api/v3/command", {"name": "TestCommand"})
        assert client.last_error is not None

    def test_last_error_cleared_after_recovery(self, client, fake_http, fake_response):
        """A successful call after a failure should reset last_error."""
        fake_http.queue("GET", fake_response(status=500, text="boom"))
        with pytest.raises(Exception):
            client._get("/fail")
        assert client.last_error is not None

        fake_http.queue("GET", fake_response(json_data={"ok": True}))
        client._get("/ok")
        assert client.last_error is None


class TestPublicLookupHelpers:
    """H14 — public helpers that replace direct _get() calls at call sites."""

    def test_lookup_by_tmdb_id_builds_correct_url(self, client, fake_http, fake_response):
        """lookup_by_tmdb_id appends the tmdb:<id> term to the endpoint."""
        fake_http.queue("GET", fake_response(json_data=[{"tmdbId": 42, "title": "Dune"}]))
        result = client.lookup_by_tmdb_id(42, endpoint="/api/v3/movie/lookup")
        assert result == [{"tmdbId": 42, "title": "Dune"}]
        _, url, _ = fake_http.calls[0]
        assert "tmdb:42" in url

    def test_lookup_by_tvdb_id_builds_correct_url(self, client, fake_http, fake_response):
        """lookup_by_tvdb_id appends the tvdb:<id> term to the endpoint."""
        fake_http.queue("GET", fake_response(json_data=[{"tvdbId": 99}]))
        result = client.lookup_by_tvdb_id(99, endpoint="/api/v3/series/lookup")
        assert result == [{"tvdbId": 99}]
        _, url, _ = fake_http.calls[0]
        assert "tvdb:99" in url

    def test_lookup_by_imdb_id_builds_correct_url(self, client, fake_http, fake_response):
        """lookup_by_imdb_id appends the imdb:<id> term to the endpoint."""
        fake_http.queue("GET", fake_response(json_data=[{"imdbId": "tt1234567"}]))
        result = client.lookup_by_imdb_id("tt1234567", endpoint="/api/v3/movie/lookup")
        assert result == [{"imdbId": "tt1234567"}]
        _, url, _ = fake_http.calls[0]
        assert "imdb:tt1234567" in url

    def test_lookup_by_term_builds_correct_url(self, client, fake_http, fake_response):
        """lookup_by_term appends the provided term to the endpoint."""
        fake_http.queue("GET", fake_response(json_data=[{"title": "Dune"}]))
        result = client.lookup_by_term("Dune", endpoint="/api/v3/movie/lookup")
        assert result == [{"title": "Dune"}]
        _, url, _ = fake_http.calls[0]
        assert "term=Dune" in url

    def test_lookup_returns_empty_list_when_none(self, client, fake_http, fake_response):
        """Helpers return [] when the upstream response is empty."""
        fake_http.queue("GET", fake_response(json_data=[]))
        assert client.lookup_by_tmdb_id(1, endpoint="/api/v3/movie/lookup") == []

    def test_get_release_returns_dict_for_valid_id(self, client, fake_http, fake_response):
        """get_release returns the item dict for a known ID."""
        fake_http.queue("GET", fake_response(json_data={"id": 7, "title": "Dune"}))
        result = client.get_release(7, endpoint="/api/v3/movie")
        assert result == {"id": 7, "title": "Dune"}

    def test_get_release_returns_none_on_error(self, client, fake_http, fake_response):
        """get_release returns None rather than raising when the ID is not found."""
        fake_http.queue("GET", fake_response(status=404, text="not found"))
        result = client.get_release(999, endpoint="/api/v3/movie")
        assert result is None
