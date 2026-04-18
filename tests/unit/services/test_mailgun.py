from unittest.mock import patch, MagicMock
import pytest
import requests as req_lib
from mediaman.services.mailgun import MailgunClient

@pytest.fixture
def client():
    return MailgunClient("example.com", "test-api-key", "notify@example.com")

class TestMailgunClient:
    @patch("mediaman.services.mailgun.requests.post")
    def test_send_email(self, mock_post, client):
        mock_post.return_value = MagicMock(status_code=200)
        client.send(to="user@example.com", subject="Test", html="<h1>Hi</h1>")
        mock_post.assert_called_once()
        call_data = mock_post.call_args[1]["data"]
        assert call_data["to"] == "user@example.com"
        assert call_data["subject"] == "Test"
        assert call_data["from"] == "notify@example.com"

    @patch("mediaman.services.mailgun.requests.post")
    def test_send_to_multiple(self, mock_post, client):
        mock_post.return_value = MagicMock(status_code=200)
        client.send_to_many(recipients=["a@test.com", "b@test.com"], subject="Report", html="<p>Content</p>")
        assert mock_post.call_count == 2

    @patch("mediaman.services.mailgun.requests.get")
    def test_test_connection(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"domain": {"name": "example.com"}})
        assert client.test_connection() is True


class TestMailgunSend:
    def test_raises_on_http_error(self):
        client = MailgunClient("example.com", "key-xxx", "noreply@example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError("401 Unauthorized")
        with patch("mediaman.services.mailgun.requests.post", return_value=mock_resp):
            with pytest.raises(req_lib.HTTPError):
                client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")

    def test_succeeds_on_200(self):
        client = MailgunClient("example.com", "key-xxx", "noreply@example.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        with patch("mediaman.services.mailgun.requests.post", return_value=mock_resp):
            client.send(to="user@example.com", subject="Test", html="<p>Hi</p>")
