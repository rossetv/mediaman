import pytest
import requests

from mediaman.services.downloads.nzbget import NzbgetClient


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
