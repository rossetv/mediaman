"""Tests for Radarr API client."""
from unittest.mock import patch, MagicMock
import pytest
from mediaman.services.radarr import RadarrClient


@pytest.fixture
def client():
    return RadarrClient("http://radarr:7878", "test-api-key")


class TestRadarrClient:
    @patch("mediaman.services.radarr.requests.get")
    def test_get_movies(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [
            {"id": 1, "title": "Test Movie", "path": "/movies/Test Movie (2024)", "monitored": True, "hasFile": True},
        ])
        movies = client.get_movies()
        assert len(movies) == 1
        assert movies[0]["title"] == "Test Movie"

    @patch("mediaman.services.radarr.requests.put")
    @patch("mediaman.services.radarr.requests.get")
    def test_unmonitor_movie(self, mock_get, mock_put, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"id": 1, "title": "Test", "monitored": True})
        mock_put.return_value = MagicMock(status_code=202)
        client.unmonitor_movie(movie_id=1)
        put_data = mock_put.call_args[1]["json"]
        assert put_data["monitored"] is False

    @patch("mediaman.services.radarr.requests.post")
    @patch("mediaman.services.radarr.requests.put")
    @patch("mediaman.services.radarr.requests.get")
    def test_remonitor_movie(self, mock_get, mock_put, mock_post, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"id": 1, "title": "Test", "monitored": False})
        mock_put.return_value = MagicMock(status_code=202)
        mock_post.return_value = MagicMock(status_code=201)
        client.remonitor_movie(movie_id=1)
        put_data = mock_put.call_args[1]["json"]
        assert put_data["monitored"] is True
        mock_post.assert_called_once()

    @patch("mediaman.services.radarr.requests.get")
    def test_test_connection(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"version": "5.0"})
        assert client.test_connection() is True


def test_search_movie_posts_moviessearch_command(monkeypatch):
    """search_movie issues POST /api/v3/command with MoviesSearch payload."""
    from mediaman.services.radarr import RadarrClient

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json

        class R:
            def raise_for_status(self):
                return None

        return R()

    monkeypatch.setattr("mediaman.services.radarr.requests.post", fake_post)

    client = RadarrClient("https://radarr.local", "key123")
    client.search_movie(42)

    assert captured["url"] == "https://radarr.local/api/v3/command"
    assert captured["headers"] == {"X-Api-Key": "key123"}
    assert captured["json"] == {"name": "MoviesSearch", "movieIds": [42]}
