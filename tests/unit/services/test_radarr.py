"""Tests for Radarr API client."""

import pytest

from mediaman.services.arr.radarr import RadarrClient


@pytest.fixture
def client():
    return RadarrClient("http://radarr:7878", "test-api-key")


def _find_call(fake_http, method):
    return next((c for c in fake_http.calls if c[0] == method.upper()), None)


class TestRadarrClient:
    def test_get_movies(self, client, fake_http, fake_response):
        fake_http.queue(
            "GET",
            fake_response(
                json_data=[
                    {
                        "id": 1,
                        "title": "Test Movie",
                        "path": "/movies/Test Movie (2024)",
                        "monitored": True,
                        "hasFile": True,
                    },
                ]
            ),
        )
        movies = client.get_movies()
        assert len(movies) == 1
        assert movies[0]["title"] == "Test Movie"

    def test_unmonitor_movie(self, client, fake_http, fake_response):
        fake_http.queue(
            "GET", fake_response(json_data={"id": 1, "title": "Test", "monitored": True})
        )
        fake_http.queue("PUT", fake_response(status=202, content=b""))
        client.unmonitor_movie(movie_id=1)
        put_call = _find_call(fake_http, "PUT")
        assert put_call[2]["json"]["monitored"] is False

    def test_remonitor_movie(self, client, fake_http, fake_response):
        fake_http.queue(
            "GET", fake_response(json_data={"id": 1, "title": "Test", "monitored": False})
        )
        fake_http.queue("PUT", fake_response(status=202, content=b""))
        fake_http.queue("POST", fake_response(status=201, json_data={}))
        client.remonitor_movie(movie_id=1)
        put_call = _find_call(fake_http, "PUT")
        post_call = _find_call(fake_http, "POST")
        assert put_call[2]["json"]["monitored"] is True
        assert post_call is not None

    def test_test_connection(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"version": "5.0"}))
        assert client.test_connection() is True

    def test_search_movie_posts_moviessearch_command(self, client, fake_http, fake_response):
        fake_http.queue("POST", fake_response(status=201, json_data={}))
        client.search_movie(42)
        post_call = _find_call(fake_http, "POST")
        assert post_call[2]["json"] == {"name": "MoviesSearch", "movieIds": [42]}
        assert "radarr:7878/api/v3/command" in post_call[1]

    def test_add_movie_sends_correct_payload(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=[{"path": "/movies"}]))
        fake_http.queue("POST", fake_response(status=201, json_data={"id": 42}))
        result = client.add_movie(tmdb_id=12345, title="Dune")
        post_call = _find_call(fake_http, "POST")
        payload = post_call[2]["json"]
        assert payload["tmdbId"] == 12345
        assert payload["monitored"] is True
        assert payload["addOptions"]["searchForMovie"] is True
        assert payload["rootFolderPath"] == "/movies"
        assert result["id"] == 42

    def test_add_movie_falls_back_to_default_root_when_rootfolder_empty(
        self, client, fake_http, fake_response
    ):
        fake_http.queue("GET", fake_response(json_data=[]))
        fake_http.queue("POST", fake_response(status=201, json_data={"id": 99}))
        client.add_movie(tmdb_id=1, title="Test")
        post_call = _find_call(fake_http, "POST")
        assert post_call[2]["json"]["rootFolderPath"] == "/movies"

    def test_get_queue_single_page_returns_all_records(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"records": [{"id": 1}], "totalRecords": 1}))
        result = client.get_queue()
        assert len(result) == 1
        assert len([c for c in fake_http.calls if c[0] == "GET"]) == 1

    def test_get_queue_multi_page_fetches_all(self, client, fake_http, fake_response):
        page1 = {"records": [{"id": 1}] * 500, "totalRecords": 501}
        page2 = {"records": [{"id": 2}], "totalRecords": 501}
        fake_http.queue("GET", fake_response(json_data=page1))
        fake_http.queue("GET", fake_response(json_data=page2))
        result = client.get_queue()
        assert len(result) == 501
        assert len([c for c in fake_http.calls if c[0] == "GET"]) == 2

    def test_get_queue_empty_returns_empty_list(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"records": [], "totalRecords": 0}))
        result = client.get_queue()
        assert result == []
