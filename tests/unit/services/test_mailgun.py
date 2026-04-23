import pytest

from mediaman.services.http_client import SafeHTTPError
from mediaman.services.mailgun import MailgunClient


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
