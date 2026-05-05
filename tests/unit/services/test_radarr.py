"""Tests for Radarr API client."""

import pytest

from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.spec import RADARR_SPEC


@pytest.fixture
def client():
    return ArrClient(RADARR_SPEC, "http://radarr:7878", "test-api-key")


def _find_call(fake_http, method):
    return next((c for c in fake_http.calls if c[0] == method.upper()), None)


class TestArrClientRadarr:
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

    def test_unmonitor_movie_noop_when_already_unmonitored(self, client, fake_http, fake_response):
        """Already-unmonitored movies short-circuit before issuing a PUT."""
        fake_http.queue(
            "GET", fake_response(json_data={"id": 1, "title": "Test", "monitored": False})
        )
        client.unmonitor_movie(movie_id=1)
        assert _find_call(fake_http, "PUT") is None

    def test_unmonitor_movie_retries_on_failed_put(self, client, fake_http, fake_response):
        """If the PUT fails, a fresh GET + retry succeeds."""
        fake_http.queue(
            "GET", fake_response(json_data={"id": 1, "title": "Test", "monitored": True})
        )
        fake_http.queue("PUT", fake_response(status=409, text="conflict"))
        fake_http.queue(
            "GET", fake_response(json_data={"id": 1, "title": "Test", "monitored": True})
        )
        fake_http.queue("PUT", fake_response(status=202, content=b""))
        client.unmonitor_movie(movie_id=1, max_retries=3)
        puts = [c for c in fake_http.calls if c[0] == "PUT"]
        assert len(puts) == 2
        assert puts[-1][2]["json"]["monitored"] is False

    def test_unmonitor_movie_aborts_when_concurrent_writer_changes_flag(
        self, client, fake_http, fake_response
    ):
        """If a concurrent writer sets monitored=False between attempts, exit cleanly."""
        fake_http.queue(
            "GET", fake_response(json_data={"id": 1, "title": "Test", "monitored": True})
        )
        fake_http.queue("PUT", fake_response(status=409, text="conflict"))
        fake_http.queue(
            "GET", fake_response(json_data={"id": 1, "title": "Test", "monitored": False})
        )
        client.unmonitor_movie(movie_id=1, max_retries=3)
        puts = [c for c in fake_http.calls if c[0] == "PUT"]
        # Only the failed first PUT — no clobbering retry.
        assert len(puts) == 1

    def test_delete_movie_sends_delete_request(self, client, fake_http, fake_response):
        """delete_movie issues a DELETE with the correct URL."""
        fake_http.queue("DELETE", fake_response(content=b""))
        client.delete_movie(movie_id=42)
        delete_call = _find_call(fake_http, "DELETE")
        assert "/api/v3/movie/42?" in delete_call[1]

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
        # GET /rootfolder + GET /qualityprofile (in that order) — both must be present.
        fake_http.queue("GET", fake_response(json_data=[{"path": "/movies"}]))
        fake_http.queue(
            "GET",
            fake_response(json_data=[{"id": 6, "name": "Any"}, {"id": 8, "name": "HD"}]),
        )
        fake_http.queue("POST", fake_response(status=201, json_data={"id": 42}))
        result = client.add_movie(tmdb_id=12345, title="Dune")
        post_call = _find_call(fake_http, "POST")
        payload = post_call[2]["json"]
        assert payload["tmdbId"] == 12345
        assert payload["monitored"] is True
        assert payload["addOptions"]["searchForMovie"] is True
        assert payload["rootFolderPath"] == "/movies"
        # Picks the lowest-numbered profile id rather than a hardcoded default.
        assert payload["qualityProfileId"] == 6
        assert result["id"] == 42

    def test_add_movie_raises_when_no_root_folder(self, client, fake_http, fake_response):
        """Empty rootfolder list now fails loudly instead of inventing /movies."""
        fake_http.queue("GET", fake_response(json_data=[]))
        with pytest.raises(RuntimeError, match="no root folders configured"):
            client.add_movie(tmdb_id=1, title="Test")

    def test_add_movie_raises_when_no_quality_profile(self, client, fake_http, fake_response):
        """Empty qualityprofile list now fails loudly rather than picking id=4."""
        fake_http.queue("GET", fake_response(json_data=[{"path": "/movies"}]))
        fake_http.queue("GET", fake_response(json_data=[]))
        with pytest.raises(RuntimeError, match="no quality profiles configured"):
            client.add_movie(tmdb_id=1, title="Test")

    def test_add_movie_rejects_non_positive_tmdb_id(self, client, fake_http):
        with pytest.raises(ValueError, match="tmdb_id must be positive"):
            client.add_movie(tmdb_id=0, title="Test")
        with pytest.raises(ValueError, match="tmdb_id must be positive"):
            client.add_movie(tmdb_id=-7, title="Test")
        # No HTTP calls should have been made — validation happens first.
        assert fake_http.calls == []

    def test_add_movie_caches_root_folder_and_quality_profile(
        self, client, fake_http, fake_response
    ):
        """Two adds in a row issue one GET each, not two."""
        fake_http.queue("GET", fake_response(json_data=[{"path": "/movies"}]))
        fake_http.queue("GET", fake_response(json_data=[{"id": 5}]))
        fake_http.queue("POST", fake_response(status=201, json_data={"id": 1}))
        fake_http.queue("POST", fake_response(status=201, json_data={"id": 2}))
        client.add_movie(tmdb_id=11, title="A")
        client.add_movie(tmdb_id=12, title="B")
        gets = [c for c in fake_http.calls if c[0] == "GET"]
        assert sum("/rootfolder" in g[1] for g in gets) == 1
        assert sum("/qualityprofile" in g[1] for g in gets) == 1

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
