"""Tests for Radarr API client."""
from unittest.mock import patch, MagicMock
import pytest
from mediaman.services.radarr import RadarrClient


@pytest.fixture
def client():
    return RadarrClient("http://radarr:7878", "test-api-key")


class TestRadarrClient:
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_get_movies(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [
            {"id": 1, "title": "Test Movie", "path": "/movies/Test Movie (2024)", "monitored": True, "hasFile": True},
        ])
        movies = client.get_movies()
        assert len(movies) == 1
        assert movies[0]["title"] == "Test Movie"

    @patch("mediaman.services.arr_client_base.requests.put")
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_unmonitor_movie(self, mock_get, mock_put, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"id": 1, "title": "Test", "monitored": True})
        mock_put.return_value = MagicMock(status_code=202)
        client.unmonitor_movie(movie_id=1)
        put_data = mock_put.call_args[1]["json"]
        assert put_data["monitored"] is False

    @patch("mediaman.services.arr_client_base.requests.post")
    @patch("mediaman.services.arr_client_base.requests.put")
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_remonitor_movie(self, mock_get, mock_put, mock_post, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"id": 1, "title": "Test", "monitored": False})
        mock_put.return_value = MagicMock(status_code=202)
        mock_post.return_value = MagicMock(status_code=201, json=lambda: {})
        client.remonitor_movie(movie_id=1)
        put_data = mock_put.call_args[1]["json"]
        assert put_data["monitored"] is True
        mock_post.assert_called_once()

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_test_connection(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"version": "5.0"})
        assert client.test_connection() is True

    @patch("mediaman.services.arr_client_base.requests.post")
    def test_search_movie_posts_moviessearch_command(self, mock_post, client):
        """search_movie issues POST /api/v3/command with MoviesSearch payload."""
        mock_post.return_value = MagicMock(status_code=201, json=lambda: {})
        client.search_movie(42)
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"] == {"name": "MoviesSearch", "movieIds": [42]}
        assert "radarr:7878/api/v3/command" in mock_post.call_args[0][0]

    @patch("mediaman.services.arr_client_base.requests.post")
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_add_movie_sends_correct_payload(self, mock_get, mock_post, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [{"path": "/movies"}])
        mock_post.return_value = MagicMock(status_code=201, json=lambda: {"id": 42})
        result = client.add_movie(tmdb_id=12345, title="Dune")
        payload = mock_post.call_args[1]["json"]
        assert payload["tmdbId"] == 12345
        assert payload["monitored"] is True
        assert payload["addOptions"]["searchForMovie"] is True
        assert payload["rootFolderPath"] == "/movies"
        assert result["id"] == 42

    @patch("mediaman.services.arr_client_base.requests.post")
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_add_movie_falls_back_to_default_root_when_rootfolder_empty(self, mock_get, mock_post, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [])
        mock_post.return_value = MagicMock(status_code=201, json=lambda: {"id": 99})
        client.add_movie(tmdb_id=1, title="Test")
        payload = mock_post.call_args[1]["json"]
        assert payload["rootFolderPath"] == "/movies"

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_get_queue_single_page_returns_all_records(self, mock_get, client):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"records": [{"id": 1}], "totalRecords": 1},
        )
        result = client.get_queue()
        assert len(result) == 1
        assert mock_get.call_count == 1

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_get_queue_multi_page_fetches_all(self, mock_get, client):
        page1 = {"records": [{"id": 1}] * 500, "totalRecords": 501}
        page2 = {"records": [{"id": 2}], "totalRecords": 501}
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: page1),
            MagicMock(status_code=200, json=lambda: page2),
        ]
        result = client.get_queue()
        assert len(result) == 501
        assert mock_get.call_count == 2

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_get_queue_empty_returns_empty_list(self, mock_get, client):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"records": [], "totalRecords": 0},
        )
        result = client.get_queue()
        assert result == []
        assert mock_get.call_count == 1
