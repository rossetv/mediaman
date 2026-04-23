import pytest

from mediaman.services.http_client import SafeHTTPError
from mediaman.services.mailgun import (
    MailgunClient,
    _validate_recipient,
    _validate_header_value,
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

    def test_send_to_multiple(self, client, fake_http, fake_response):
        fake_http.default(fake_response(status=200, content=b""))
        client.send_to_many(recipients=["a@test.com", "b@test.com"], subject="Report", html="<p>Content</p>")
        assert len(fake_http.calls) == 2

    def test_test_connection(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"domain": {"name": "example.com"}}))
        assert client.test_connection() is True


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


class TestMailgunValidation:
    """Tests for H55: recipient and header injection guards."""

    def test_valid_address_passes(self):
        _validate_recipient("user@example.com")  # must not raise

    def test_empty_address_rejected(self):
        with pytest.raises(ValueError, match="Invalid recipient"):
            _validate_recipient("")

    def test_address_without_at_rejected(self):
        with pytest.raises(ValueError, match="Invalid recipient"):
            _validate_recipient("notanemail")

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
            client.send(to="user@example.com", subject="Hi\r\nBcc: evil@example.com", html="<p>Hi</p>")
        assert len(fake_http.calls) == 0
