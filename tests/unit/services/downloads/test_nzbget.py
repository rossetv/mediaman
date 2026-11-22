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
    """JSON-RPC errors must raise NzbgetError, not silently return {}."""

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


class TestNzbgetMalformedReply:
    """A reply that is not a well-formed JSON-RPC result must raise, so callers do not
    read it as an idle server or an empty queue."""

    @pytest.mark.parametrize(
        "body",
        [{}, {"result": None}, {"result": {"NZBName": "x"}}, [], "ok"],
        ids=["empty-object", "null-result", "object-result", "list-body", "string-body"],
    )
    def test_malformed_queue_reply_raises(self, client, fake_http, fake_response, body):
        fake_http.queue("POST", fake_response(json_data=body))
        with pytest.raises(NzbgetError):
            client.get_queue()

    @pytest.mark.parametrize(
        "body",
        [{}, {"result": None}, {"result": []}, ["x"]],
        ids=["empty-object", "null-result", "list-result", "list-body"],
    )
    def test_malformed_status_reply_raises(self, client, fake_http, fake_response, body):
        fake_http.queue("POST", fake_response(json_data=body))
        with pytest.raises(NzbgetError):
            client.get_status()

    def test_empty_queue_is_not_an_error(self, client, fake_http, fake_response):
        fake_http.queue("POST", fake_response(json_data={"result": []}))
        assert client.get_queue() == []


class TestNzbgetCheckReachable:
    def test_returns_none_when_status_answers(self, client, fake_http, fake_response):
        fake_http.queue("POST", fake_response(json_data={"result": {"ServerStandBy": False}}))
        assert client.check_reachable() is None

    def test_raises_the_http_error_for_a_401(self, client, fake_http, fake_response):
        from mediaman.services.infra import SafeHTTPError

        fake_http.queue("POST", fake_response(status=401, text="no"))
        with pytest.raises(SafeHTTPError) as info:
            client.check_reachable()
        assert info.value.status_code == 401

    def test_raises_nzbget_error_for_a_reply_that_is_not_a_status(
        self, client, fake_http, fake_response
    ):
        fake_http.queue("POST", fake_response(json_data=["not", "a", "status"]))
        with pytest.raises(NzbgetError):
            client.check_reachable()
        fake_http.queue("POST", fake_response(json_data=["not", "a", "status"]))
        assert client.is_reachable() is False
