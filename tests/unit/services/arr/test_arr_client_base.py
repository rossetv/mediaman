"""Tests for the shared ArrClient HTTP plumbing.

These tests cover only the kind-agnostic plumbing layer (``_get``/``_put``/
``_post``/``_delete``, ``is_reachable``, ``last_error``, the lookup helpers
and ``get_release``).  Either spec is valid here because none of these
methods read :attr:`ArrClient.spec`; ``SONARR_SPEC`` is used arbitrarily.
"""

from __future__ import annotations

import pytest

from mediaman.services.arr.base import ArrClient, ArrUpstreamError
from mediaman.services.arr.spec import SONARR_SPEC
from mediaman.services.infra import SafeHTTPError


@pytest.fixture
def client() -> ArrClient:
    return ArrClient(SONARR_SPEC, "http://arr.local:7878", "test-api-key")


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
        c = ArrClient(SONARR_SPEC, "http://arr.local/", "key")
        assert not c._url.endswith("/")

    def test_api_key_stored_in_header(self):
        """API key is forwarded verbatim in the X-Api-Key header."""
        c = ArrClient(SONARR_SPEC, "http://arr.local", "my-secret-key")
        assert c._headers["X-Api-Key"] == "my-secret-key"


class TestLastError:
    """H35 -- last_error is populated on failure and cleared on success."""

    def test_last_error_initially_none(self):
        """last_error is None before any call is made."""
        c = ArrClient(SONARR_SPEC, "http://arr.local", "key")
        assert c.last_error is None

    def test_split_timeout_applied(self):
        """SafeHTTPClient is configured with the split connect/read timeout."""
        from mediaman.services.arr.base import _ARR_TIMEOUT_SECONDS

        c = ArrClient(SONARR_SPEC, "http://arr.local", "key")
        assert c._http._default_timeout == _ARR_TIMEOUT_SECONDS
        assert _ARR_TIMEOUT_SECONDS == (5.0, 30.0)

    def test_last_error_cleared_on_success(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"status": "ok"}))
        client._get("/api/v3/system/status")
        assert client.last_error is None

    def test_last_error_set_on_get_failure(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(status=500, text="boom"))
        with pytest.raises(SafeHTTPError):
            client._get("/api/v3/system/status")
        assert client.last_error is not None
        assert len(client.last_error) > 0

    def test_last_error_set_on_post_failure(self, client, fake_http, fake_response):
        fake_http.queue("POST", fake_response(status=503, text="unavailable"))
        with pytest.raises(SafeHTTPError):
            client._post("/api/v3/command", {"name": "TestCommand"})
        assert client.last_error is not None

    def test_last_error_cleared_after_recovery(self, client, fake_http, fake_response):
        """A successful call after a failure should reset last_error."""
        fake_http.queue("GET", fake_response(status=500, text="boom"))
        with pytest.raises(SafeHTTPError):
            client._get("/fail")
        assert client.last_error is not None

        fake_http.queue("GET", fake_response(json_data={"ok": True}))
        client._get("/ok")
        assert client.last_error is None


class TestPostNullBodyGuard:
    """B1 — _post must fail closed on a null upstream body like _get does.

    Add-flow callers (add_series/add_movie/add_series_with_seasons) cast the
    POST result and read a field off it; a literal ``null`` body would
    otherwise surface as a bare AttributeError, not a domain error.
    """

    def test_post_raises_arr_upstream_error_on_null_body(self, client, fake_http, fake_response):
        # A 200 with a literal JSON ``null`` body — resp.json() returns None.
        fake_http.queue("POST", fake_response(status=200, content=b"null"))
        with pytest.raises(ArrUpstreamError):
            client._post("/api/v3/series", {"title": "X"})

    def test_post_returns_dict_on_normal_body(self, client, fake_http, fake_response):
        fake_http.queue("POST", fake_response(json_data={"id": 7, "title": "X"}))
        assert client._post("/api/v3/series", {"title": "X"}) == {"id": 7, "title": "X"}


class TestPublicLookupHelpers:
    """H14 — public helpers that replace direct _get() calls at call sites."""

    def test_lookup_by_tmdb_id_builds_correct_url(self, client, fake_http, fake_response):
        """lookup_by_tmdb_id appends the tmdb:<id> term to the endpoint."""
        fake_http.queue("GET", fake_response(json_data=[{"tmdbId": 42, "title": "Dune"}]))
        result = client.lookup_by_tmdb_id(42, endpoint="/api/v3/movie/lookup")
        assert result == [{"tmdbId": 42, "title": "Dune"}]
        _, url, _ = fake_http.calls[0]
        assert "tmdb:42" in url

    def test_lookup_by_term_builds_correct_url(self, client, fake_http, fake_response):
        """lookup_by_term appends the provided term to the endpoint."""
        fake_http.queue("GET", fake_response(json_data=[{"title": "Dune"}]))
        result = client.lookup_by_term("Dune", endpoint="/api/v3/movie/lookup")
        assert result == [{"title": "Dune"}]
        _, url, _ = fake_http.calls[0]
        assert "term=Dune" in url

    def test_lookup_by_term_url_encodes_special_characters(self, client, fake_http, fake_response):
        """M1 — lookup_by_term URL-encodes the term inside the helper.

        A term with ``&`` or a space must not inject a spurious query
        parameter into the upstream request; the raw characters must be
        percent-encoded so the whole title stays in the ``term`` value.
        """
        fake_http.queue("GET", fake_response(json_data=[]))
        client.lookup_by_term("Tom & Jerry: The Movie", endpoint="/api/v3/movie/lookup")
        _, url, _ = fake_http.calls[0]
        # The raw '&' and spaces must be gone from the query — encoded instead.
        assert "term=Tom%20%26%20Jerry" in url or "term=Tom+%26+Jerry" in url
        assert "&Jerry" not in url  # no injected spurious parameter

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
