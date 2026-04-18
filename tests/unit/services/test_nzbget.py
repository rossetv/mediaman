from unittest.mock import patch, MagicMock
import pytest
from mediaman.services.nzbget import NzbgetClient

@pytest.fixture
def client():
    return NzbgetClient("http://localhost:6789", "user", "pass")

class TestNzbgetClient:
    @patch("mediaman.services.nzbget.requests.post")
    def test_get_status(self, mock_post, client):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"result": {"RemainingSizeMB": 1024, "DownloadRate": 44_564_480}})
        status = client.get_status()
        assert status["RemainingSizeMB"] == 1024

    @patch("mediaman.services.nzbget.requests.post")
    def test_get_queue(self, mock_post, client):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"result": [
            {"NZBName": "Test.Movie.2024", "Status": "Downloading", "FileSizeMB": 4096, "RemainingSizeMB": 2048, "Category": "movies"},
        ]})
        queue = client.get_queue()
        assert len(queue) == 1
        assert queue[0]["NZBName"] == "Test.Movie.2024"

    @patch("mediaman.services.nzbget.requests.post")
    def test_test_connection(self, mock_post, client):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"result": {"ServerStandBy": False}})
        assert client.test_connection() is True

    @patch("mediaman.services.nzbget.requests.post")
    def test_test_connection_failure(self, mock_post, client):
        mock_post.side_effect = Exception("refused")
        assert client.test_connection() is False
