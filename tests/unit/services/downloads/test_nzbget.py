from __future__ import annotations

import pytest
import requests

from mediaman.services.downloads.nzbget import NzbgetClient, NzbgetError


@pytest.fixture
def client():
    return NzbgetClient("http://localhost:6789", "user", "pass")


class TestNzbgetClient:
    def test_get_status(self, client, fake_http, fake_response):
        fake_http.queue(
            "POST",
            fake_response(
                json_data={"result": {"RemainingSizeMB": 1024, "DownloadRate": 44_564_480}}
            ),
        )
        status = client.get_status()
        assert status["RemainingSizeMB"] == 1024

    def test_get_queue(self, client, fake_http, fake_response):
        fake_http.queue(
            "POST",
            fake_response(
                json_data={
                    "result": [
                        {
                            "NZBName": "Test.Movie.2024",
                            "Status": "Downloading",
                            "FileSizeMB": 4096,
                            "RemainingSizeMB": 2048,
                            "Category": "movies",
                        },
                    ]
                }
            ),
        )
        queue = client.get_queue()
        assert len(queue) == 1
        assert queue[0]["NZBName"] == "Test.Movie.2024"

    def test_test_connection(self, client, fake_http, fake_response):
        fake_http.queue(
            "POST",
            fake_response(json_data={"result": {"ServerStandBy": False}}),
        )
        assert client.is_reachable() is True

    def test_test_connection_failure(self, client, fake_http):
        fake_http.raise_on("POST", requests.ConnectionError("refused"))
        assert client.is_reachable() is False


class TestNzbgetJsonRpcError:
    """B4/M6 — JSON-RPC errors must raise NzbgetError, not silently return {}."""

    def test_rpc_error_raises_nzbget_error(self, client, fake_http, fake_response):
        """A JSON-RPC error object in the response must raise NzbgetError.

        NZBGet returns ``{"error": {"code": ..., "message": ...}, "result": null}``
        for authentication failures and method-not-found.  Previously this
        silently returned ``{}`` and callers saw an empty queue with no logs.
        """
        fake_http.queue(
            "POST",
            fake_response(
                json_data={
                    "error": {"code": -32601, "message": "No such method"},
                    "result": None,
                }
            ),
        )
        with pytest.raises(NzbgetError, match="No such method"):
            client.get_status()

    def test_rpc_auth_error_raises_nzbget_error(self, client, fake_http, fake_response):
        """Authentication failure returns an error object; must raise NzbgetError."""
        fake_http.queue(
            "POST",
            fake_response(
                json_data={
                    "error": {"code": 401, "message": "Access denied"},
                    "result": None,
                }
            ),
        )
        with pytest.raises(NzbgetError, match="Access denied"):
            client.get_queue()

    def test_rpc_error_makes_is_reachable_return_false(self, client, fake_http, fake_response):
        """NzbgetError from an RPC error must not propagate through is_reachable."""
        fake_http.queue(
            "POST",
            fake_response(
                json_data={
                    "error": {"code": 401, "message": "Access denied"},
                    "result": None,
                }
            ),
        )
        assert client.is_reachable() is False

    def test_successful_response_does_not_raise(self, client, fake_http, fake_response):
        """A clean ``{"error": null, "result": {...}}`` must not raise."""
        fake_http.queue(
            "POST",
            fake_response(json_data={"error": None, "result": {"DownloadRate": 0}}),
        )
        # Should not raise — falsy error field is ignored.
        status = client.get_status()
        assert status["DownloadRate"] == 0
