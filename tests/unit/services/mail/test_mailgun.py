from __future__ import annotations

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from mediaman.services.infra import SafeHTTPError
from mediaman.services.mail.mailgun import (
    MailgunClient,
    _validate_header_value,
    _validate_recipient,
)


@pytest.fixture
def client():
    return MailgunClient("example.com", "test-api-key", "notify@example.com")


class TestMailgunClient:
    def test_send_email(self, client, fake_http, fake_response):
        fake_http.queue("POST", fake_response(status=200, content=b""))
        client.send(to="user@example.com", subject="Test", html="<h1>Hi</h1>")
        assert len(fake_http.calls) == 1
        _, _, kwargs = fake_http.calls[0]
        data = kwargs["data"]
        assert data["to"] == "user@example.com"
        assert data["subject"] == "Test"
        assert data["from"] == "notify@example.com"

    def test_send_adds_a_plain_text_part_with_the_links_kept(
        self, client, fake_http, fake_response
    ):
        fake_http.queue("POST", fake_response(status=200, content=b""))
        html = (
            "<html><head><title>T</title><style>p{color:red}</style></head><body>"
            '<div>Hello <b>there</b></div><a href="https://m.example/keep/tok?x=1&amp;y=2">Keep</a>'
            "</body></html>"
        )
        client.send(to="user@example.com", subject="Test", html=html)
        data = fake_http.calls[0][2]["data"]
        assert data["html"] == html
        assert "Hello there" in data["text"]
        assert "Keep (https://m.example/keep/tok?x=1&y=2)" in data["text"]
        assert "color:red" not in data["text"]
        assert "<" not in data["text"]

    def test_send_advertises_the_unsubscribe_url_when_given(self, client, fake_http, fake_response):
        fake_http.queue("POST", fake_response(status=200, content=b""))
        url = "https://m.example/unsubscribe?token=abc.def%3D"
        client.send(to="user@example.com", subject="Test", html="<p>Hi</p>", unsubscribe_url=url)
        data = fake_http.calls[0][2]["data"]
        assert data["h:List-Unsubscribe"] == f"<{url}>"

    def test_send_without_an_unsubscribe_url_sets_no_header(self, client, fake_http, fake_response):
        fake_http.queue("POST", fake_response(status=200, content=b""))
        client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")
        assert "h:List-Unsubscribe" not in fake_http.calls[0][2]["data"]

    def test_send_refuses_an_unsubscribe_url_that_could_inject_a_header(self, client):
        with pytest.raises(ValueError):
            client.send(
                to="user@example.com",
                subject="Test",
                html="<p>Hi</p>",
                unsubscribe_url="https://m.example/u\r\nBcc: x@example.com",
            )

    def test_test_connection(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"domain": {"name": "example.com"}}))
        assert client.is_reachable() is True


class TestMailgunSend:
    def test_raises_on_http_error(self, fake_http, fake_response):
        client = MailgunClient("example.com", "key-xxx", "noreply@example.com")
        # Return 401 for both EU and US — non-retryable after region switch.
        fake_http.default(fake_response(status=401, text="no"))
        with pytest.raises(SafeHTTPError):
            client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")

    def test_succeeds_on_200(self, fake_http, fake_response):
        client = MailgunClient("example.com", "key-xxx", "noreply@example.com")
        fake_http.queue("POST", fake_response(status=200, content=b""))
        client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")

    def test_401_not_retried_against_alternate_region(self, fake_http, fake_response):
        """A 401 response must not trigger a region fallback — it means bad credentials."""
        client = MailgunClient("example.com", "key-xxx", "noreply@example.com")
        fake_http.queue("POST", fake_response(status=401, text="Forbidden"))
        with pytest.raises(SafeHTTPError) as exc_info:
            client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")
        # Only one POST attempted — no retry against the alternate region.
        assert len([c for c in fake_http.calls if c[0] == "POST"]) == 1
        assert exc_info.value.status_code == 401

    def test_404_retried_against_alternate_region(self, fake_http, fake_response):
        """A 404 on the first region triggers a retry against the alternate region."""
        client = MailgunClient("example.com", "key-xxx", "noreply@example.com", region="eu")
        fake_http.queue("POST", fake_response(status=404, text="domain not found"))
        fake_http.queue("POST", fake_response(status=200, content=b""))
        client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")
        assert len([c for c in fake_http.calls if c[0] == "POST"]) == 2


class TestMailgunRetryPolicy:
    """The mailgun POST shares ``dispatch_loop`` with every other outbound caller,
    opting into full-jitter backoff and retrying only when the message cannot yet have
    been sent: a connection that never opened, or a 429.  These tests pin the
    user-visible behaviour.

    All tests stub out the sleep and jitter sources so the suite stays
    deterministic and fast.
    """

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        # Both the retry module's ``time.sleep`` and ``random.uniform``
        # are exercised by every retry path; replace both with no-op /
        # deterministic stubs so we test orchestration, not timing.
        monkeypatch.setattr("mediaman.services.infra.http.retry.time.sleep", lambda _: None)
        monkeypatch.setattr("mediaman.services.infra.http.retry.random.uniform", lambda _a, _b: 0.0)

    def _client(self) -> MailgunClient:
        return MailgunClient("example.com", "key-xxx", "noreply@example.com", region="eu")

    def test_send_retries_on_429(self, fake_http, fake_response):
        client = self._client()
        fake_http.queue("POST", fake_response(status=429, text="Too Many Requests"))
        fake_http.queue("POST", fake_response(status=200, content=b""))
        client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")
        assert len([c for c in fake_http.calls if c[0] == "POST"]) == 2

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_send_does_not_retry_a_5xx(self, status, fake_http, fake_response):
        """A 5xx may follow a send Mailgun already accepted; resending could deliver twice."""
        client = self._client()
        fake_http.queue("POST", fake_response(status=status, text="oops"))
        fake_http.queue("POST", fake_response(status=200, content=b""))
        with pytest.raises(SafeHTTPError) as exc_info:
            client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")
        assert exc_info.value.status_code == status
        assert len([c for c in fake_http.calls if c[0] == "POST"]) == 1

    @pytest.mark.parametrize(
        "failure",
        [
            requests.exceptions.ConnectTimeout("no route"),
            requests.exceptions.ConnectionError(
                MaxRetryError(None, "/", NewConnectionError(None, "refused"))
            ),
        ],
        ids=["connect_timeout", "connection_refused"],
    )
    def test_send_retries_when_the_connection_never_opened(self, failure, fake_http, fake_response):
        client = self._client()
        outcomes = [failure, fake_response(status=200, content=b"")]

        def handler(method, url, **kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        fake_http.handler(handler)
        client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")
        assert len([c for c in fake_http.calls if c[0] == "POST"]) == 2

    @pytest.mark.parametrize(
        "failure",
        [
            requests.exceptions.ReadTimeout("slow"),
            requests.exceptions.ConnectionError(
                ProtocolError("Connection aborted.", ConnectionResetError())
            ),
            requests.exceptions.ChunkedEncodingError("cut"),
        ],
        ids=["read_timeout", "reset_after_send", "chunked_encoding"],
    )
    def test_send_does_not_retry_once_the_request_may_have_been_sent(self, failure, fake_http):
        client = self._client()
        fake_http.raise_on("POST", failure)
        with pytest.raises((SafeHTTPError, requests.RequestException)):
            client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")
        assert len([c for c in fake_http.calls if c[0] == "POST"]) == 1

    def test_send_does_not_retry_401(self, fake_http, fake_response):
        """401 is not in the retryable set and propagates immediately."""
        client = self._client()
        fake_http.queue("POST", fake_response(status=401, text="Unauthorised"))
        with pytest.raises(SafeHTTPError) as exc_info:
            client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")
        assert exc_info.value.status_code == 401
        # Only one POST attempted — no retry against the alternate region either.
        post_calls = [c for c in fake_http.calls if c[0] == "POST"]
        assert len(post_calls) == 1

    def test_send_exhausts_retries_on_persistent_429(self, fake_http, fake_response):
        """Three 429 responses exhaust the attempt budget and propagate.

        Mailgun only falls back to the alternate region on 404 (a domain
        registered in the other region); 429 propagates after the
        per-region retry budget is spent, so the total POST count is
        ``1 region × 3 attempts == 3``.
        """
        client = self._client()
        fake_http.default(fake_response(status=429, text="Too Many Requests"))
        with pytest.raises(SafeHTTPError) as exc_info:
            client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")
        assert exc_info.value.status_code == 429
        post_calls = [c for c in fake_http.calls if c[0] == "POST"]
        assert len(post_calls) == 3


class TestMailgunValidation:
    """Tests for recipient and header injection guards."""

    def test_valid_address_passes(self):
        _validate_recipient("user@example.com")  # must not raise

    @pytest.mark.parametrize(
        ("address", "match"),
        [
            ("", "Invalid email address"),
            ("notanemail", "Invalid email address"),
        ],
        ids=["empty", "no-at-sign"],
    )
    def test_invalid_address_rejected(self, address, match):
        with pytest.raises(ValueError, match=match):
            _validate_recipient(address)

    def test_newline_in_address_rejected(self):
        """CR/LF in a recipient address would enable header injection."""
        with pytest.raises(ValueError, match="illegal characters"):
            _validate_recipient("user@example.com\r\nBcc: evil@attacker.com")

    def test_newline_in_subject_rejected(self):
        """CR/LF in the subject line would enable header injection."""
        with pytest.raises(ValueError, match="illegal characters"):
            _validate_header_value("Legit Subject\r\nBcc: evil@attacker.com", "subject")

    def test_send_rejects_invalid_recipient(self, fake_http, fake_response):
        """send() must validate the recipient before making a network call."""
        client = MailgunClient("example.com", "key-xxx", "noreply@example.com")
        with pytest.raises(ValueError):
            client.send(to="notanemail", subject="Test", html="<p>Hi</p>")
        assert len(fake_http.calls) == 0

    def test_send_rejects_header_injected_subject(self, fake_http, fake_response):
        """send() must reject a subject containing CR/LF before any network call."""
        client = MailgunClient("example.com", "key-xxx", "noreply@example.com")
        with pytest.raises(ValueError):
            client.send(
                to="user@example.com", subject="Hi\r\nBcc: evil@example.com", html="<p>Hi</p>"
            )
        assert len(fake_http.calls) == 0


class TestMailgunCheckReachable:
    def test_returns_none_when_a_region_answers(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"domain": {"name": "example.com"}}))
        assert client.check_reachable() is None

    def test_a_second_region_that_answers_clears_the_first_failure(
        self, client, fake_http, fake_response
    ):
        fake_http.queue("GET", fake_response(status=404, text="no"))
        fake_http.queue("GET", fake_response(json_data={"domain": {}}))
        assert client.check_reachable() is None

    def test_raises_the_last_failure_when_every_region_failed(
        self, client, fake_http, fake_response
    ):
        fake_http.queue("GET", fake_response(status=404, text="no"))
        fake_http.queue("GET", fake_response(status=401, text="no"))
        with pytest.raises(SafeHTTPError) as info:
            client.check_reachable()
        assert info.value.status_code == 401

    def test_a_rejected_key_wins_over_the_other_regions_404(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(status=401, text="no"))
        fake_http.queue("GET", fake_response(status=404, text="no"))
        with pytest.raises(SafeHTTPError) as info:
            client.check_reachable()
        assert info.value.status_code == 401

    def test_raises_the_transport_error_when_every_region_is_unreachable(self, client, fake_http):
        fake_http.raise_on("GET", requests.ConnectionError("down"))
        with pytest.raises((requests.RequestException, SafeHTTPError)):
            client.check_reachable()
        assert client.is_reachable() is False
