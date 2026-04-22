"""Tests for the shared ArrClient HTTP base class."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.services.arr_client_base import ArrClient


class _TestClient(ArrClient):
    """Minimal concrete subclass for testing ArrClient directly."""


@pytest.fixture
def client() -> _TestClient:
    return _TestClient("http://arr.local:7878", "test-api-key")


class TestGetMethod:
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_get_returns_json(self, mock_get, client):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "ok"},
        )
        result = client._get("/api/v3/system/status")
        assert result == {"status": "ok"}

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_get_raises_on_http_error(self, mock_get, client):
        resp = MagicMock(status_code=404)
        resp.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
        mock_get.return_value = resp
        with pytest.raises(requests.HTTPError):
            client._get("/api/v3/movie")

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_get_passes_api_key_header(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [])
        client._get("/api/v3/movie")
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["X-Api-Key"] == "test-api-key"


class TestPutMethod:
    @patch("mediaman.services.arr_client_base.requests.put")
    def test_put_sends_json_body(self, mock_put, client):
        mock_put.return_value = MagicMock(status_code=202)
        client._put("/api/v3/movie/1", data={"key": "val"})
        _, kwargs = mock_put.call_args
        assert kwargs["json"] == {"key": "val"}

    @patch("mediaman.services.arr_client_base.requests.put")
    def test_put_includes_correct_url(self, mock_put, client):
        mock_put.return_value = MagicMock(status_code=202)
        client._put("/api/v3/movie/42", data={})
        url = mock_put.call_args[0][0]
        assert url == "http://arr.local:7878/api/v3/movie/42"


class TestDeleteMethod:
    @patch("mediaman.services.arr_client_base.requests.delete")
    def test_delete_sends_request(self, mock_delete, client):
        mock_delete.return_value = MagicMock(status_code=200)
        client._delete("/api/v3/movie/1")
        mock_delete.assert_called_once()

    @patch("mediaman.services.arr_client_base.requests.delete")
    def test_delete_includes_correct_url(self, mock_delete, client):
        mock_delete.return_value = MagicMock(status_code=200)
        client._delete("/api/v3/movie/99")
        url = mock_delete.call_args[0][0]
        assert url == "http://arr.local:7878/api/v3/movie/99"


class TestTestConnection:
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_test_connection_true(self, mock_get, client):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"version": "5.0"},
        )
        assert client.test_connection() is True

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_test_connection_false_on_exception(self, mock_get, client):
        mock_get.side_effect = requests.ConnectionError("unreachable")
        assert client.test_connection() is False

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_test_connection_false_on_http_error(self, mock_get, client):
        resp = MagicMock(status_code=401)
        resp.raise_for_status.side_effect = requests.HTTPError("401 Unauthorised")
        mock_get.return_value = resp
        assert client.test_connection() is False


class TestConstructor:
    def test_trailing_slash_stripped_from_url(self):
        c = _TestClient("http://arr.local/", "key")
        assert not c._url.endswith("/")

    def test_api_key_stored_in_header(self):
        """API key is forwarded verbatim in the X-Api-Key header."""
        c = _TestClient("http://arr.local", "my-secret-key")
        assert c._headers["X-Api-Key"] == "my-secret-key"
