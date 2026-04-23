"""Tests for Sonarr API client."""
import pytest

from mediaman.services.sonarr import SonarrClient


@pytest.fixture
def client():
    return SonarrClient("http://sonarr:8989", "test-api-key")


def _calls(fake_http, method):
    return [c for c in fake_http.calls if c[0] == method.upper()]


class TestSonarrClient:
    def test_get_series(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=[
            {"id": 1, "title": "Modern Family", "path": "/tv/Modern Family", "seasons": [{"seasonNumber": 1, "monitored": True}]},
        ]))
        series = client.get_series()
        assert len(series) == 1
        assert series[0]["title"] == "Modern Family"

    def test_unmonitor_season(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={
            "id": 1, "title": "Modern Family",
            "seasons": [{"seasonNumber": 1, "monitored": True}, {"seasonNumber": 2, "monitored": True}],
        }))
        fake_http.queue("PUT", fake_response(status=202, content=b""))
        client.unmonitor_season(series_id=1, season_number=2)
        put_call = _calls(fake_http, "PUT")[0]
        season_2 = next(s for s in put_call[2]["json"]["seasons"] if s["seasonNumber"] == 2)
        assert season_2["monitored"] is False

    def test_remonitor_season(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={
            "id": 1, "title": "Test", "seasons": [{"seasonNumber": 1, "monitored": False}],
        }))
        fake_http.queue("PUT", fake_response(status=202, content=b""))
        fake_http.queue("POST", fake_response(status=201, json_data={}))
        client.remonitor_season(series_id=1, season_number=1)
        put_call = _calls(fake_http, "PUT")[0]
        assert put_call[2]["json"]["seasons"][0]["monitored"] is True
        assert len(_calls(fake_http, "POST")) == 1

    def test_test_connection(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"version": "4.0"}))
        assert client.test_connection() is True

    def test_test_connection_failure(self, client, fake_http):
        import requests
        fake_http.raise_on("GET", requests.ConnectionError("Connection refused"))
        assert client.test_connection() is False

    def test_search_series_posts_seriessearch_command(self, client, fake_http, fake_response):
        fake_http.queue("POST", fake_response(status=201, json_data={}))
        client.search_series(7)
        post = _calls(fake_http, "POST")[0]
        assert post[2]["json"] == {"name": "SeriesSearch", "seriesId": 7}
        assert "sonarr:8989/api/v3/command" in post[1]

    def test_get_episodes_returns_episode_list(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=[{"id": 1, "airDateUtc": "2099-01-01T00:00:00Z"}]))
        result = client.get_episodes(42)
        get_call = _calls(fake_http, "GET")[0]
        assert "episode?seriesId=42" in get_call[1]
        assert result[0]["id"] == 1

    def test_get_episodes_returns_empty_list_on_non_list_response(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"error": "unexpected"}))
        assert client.get_episodes(1) == []

    def test_add_series_sends_correct_payload(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=[{"path": "/tv"}]))
        fake_http.queue("POST", fake_response(status=201, json_data={"id": 10}))
        result = client.add_series(tvdb_id=999, title="Severance")
        post = _calls(fake_http, "POST")[0]
        payload = post[2]["json"]
        assert payload["tvdbId"] == 999
        assert payload["monitored"] is True
        assert payload["addOptions"]["searchForMissingEpisodes"] is True
        assert payload["rootFolderPath"] == "/tv"
        assert result["id"] == 10

    def test_add_series_falls_back_to_default_root(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=[]))
        fake_http.queue("POST", fake_response(status=201, json_data={"id": 5}))
        client.add_series(tvdb_id=1, title="Test")
        post = _calls(fake_http, "POST")[0]
        assert post[2]["json"]["rootFolderPath"] == "/tv"

    def test_get_queue_single_page(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"records": [{"id": 1}], "totalRecords": 1}))
        result = client.get_queue()
        assert len(result) == 1
        assert len(_calls(fake_http, "GET")) == 1

    def test_get_queue_multi_page(self, client, fake_http, fake_response):
        page1 = {"records": [{"id": 1}] * 500, "totalRecords": 501}
        page2 = {"records": [{"id": 2}], "totalRecords": 501}
        fake_http.queue("GET", fake_response(json_data=page1))
        fake_http.queue("GET", fake_response(json_data=page2))
        result = client.get_queue()
        assert len(result) == 501

    def test_get_queue_empty(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"records": [], "totalRecords": 0}))
        assert client.get_queue() == []


class TestLookupSeriesByTmdb:
    def test_returns_first_hit(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=[
            {"tvdbId": 101, "title": "Arcane"},
            {"tvdbId": 102, "title": "Other"},
        ]))
        result = client.lookup_series_by_tmdb(12345)
        assert result["tvdbId"] == 101
        get_call = _calls(fake_http, "GET")[0]
        assert "term=tmdb:12345" in get_call[1]

    def test_returns_none_on_empty(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=[]))
        assert client.lookup_series_by_tmdb(12345) is None

    def test_returns_none_on_exception(self, client, fake_http):
        fake_http.raise_on("GET", Exception("boom"))
        assert client.lookup_series_by_tmdb(12345) is None


class TestAddSeriesWithSeasons:
    def test_sends_correct_monitored_flags(self, client, fake_http, fake_response):
        lookup_result = [{
            "tvdbId": 999,
            "title": "Breaking Bad",
            "seasons": [
                {"seasonNumber": 0}, {"seasonNumber": 1}, {"seasonNumber": 2},
                {"seasonNumber": 3}, {"seasonNumber": 4}, {"seasonNumber": 5},
            ],
        }]
        fake_http.queue("GET", fake_response(json_data=[{"path": "/tv"}]))
        fake_http.queue("GET", fake_response(json_data=lookup_result))
        fake_http.queue("POST", fake_response(status=201, json_data={"id": 42}))
        fake_http.queue("POST", fake_response(status=201, json_data={}))

        client.add_series_with_seasons(
            tvdb_id=999, title="Breaking Bad",
            monitored_seasons=[1, 2], search_seasons=[2],
        )

        posts = _calls(fake_http, "POST")
        add_body = posts[0][2]["json"]
        assert add_body["addOptions"]["searchForMissingEpisodes"] is False
        by_num = {s["seasonNumber"]: s["monitored"] for s in add_body["seasons"]}
        assert by_num == {0: False, 1: True, 2: True, 3: False, 4: False, 5: False}

        search_calls = [c for c in posts if "/api/v3/command" in c[1]]
        assert len(search_calls) == 1
        assert search_calls[0][2]["json"] == {
            "name": "SeasonSearch", "seriesId": 42, "seasonNumber": 2,
        }

    def test_no_search_when_search_seasons_empty(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=[{"path": "/tv"}]))
        fake_http.queue("GET", fake_response(json_data=[{
            "tvdbId": 10, "title": "X",
            "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
        }]))
        fake_http.queue("POST", fake_response(status=201, json_data={"id": 7}))

        client.add_series_with_seasons(
            tvdb_id=10, title="X",
            monitored_seasons=[1, 2], search_seasons=[],
        )

        command_calls = [c for c in _calls(fake_http, "POST") if "/api/v3/command" in c[1]]
        assert command_calls == []


class TestDeleteEpisodeFiles:
    """H46 — bulk DELETE via /api/v3/episodefile/bulk with 404 fallback."""

    def test_bulk_delete_sends_single_request(self, client, fake_http, fake_response):
        """All episode-file ids for the season are sent in one DELETE."""
        fake_http.queue("GET", fake_response(json_data=[
            {"id": 1, "seasonNumber": 1},
            {"id": 2, "seasonNumber": 1},
            {"id": 3, "seasonNumber": 2},  # different season — must be excluded
        ]))
        fake_http.queue("DELETE", fake_response(content=b""))

        client.delete_episode_files(series_id=10, season_number=1)

        delete_calls = _calls(fake_http, "DELETE")
        assert len(delete_calls) == 1
        assert delete_calls[0][2]["json"] == {"episodeFileIds": [1, 2]}

    def test_bulk_delete_sends_to_bulk_endpoint(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data=[{"id": 5, "seasonNumber": 3}]))
        fake_http.queue("DELETE", fake_response(content=b""))

        client.delete_episode_files(series_id=7, season_number=3)

        _, url, _ = _calls(fake_http, "DELETE")[0]
        assert "episodefile/bulk" in url

    def test_404_on_bulk_falls_back_to_serial(self, client, fake_http, fake_response):
        """When bulk endpoint returns 404, fall back to one DELETE per file."""
        fake_http.queue("GET", fake_response(json_data=[
            {"id": 10, "seasonNumber": 1},
            {"id": 11, "seasonNumber": 1},
        ]))
        fake_http.queue("DELETE", fake_response(status=404, text="not found"))
        fake_http.queue("DELETE", fake_response(content=b""))
        fake_http.queue("DELETE", fake_response(content=b""))

        client.delete_episode_files(series_id=5, season_number=1)

        delete_calls = _calls(fake_http, "DELETE")
        # First call was the bulk attempt (404), then two serial calls
        assert len(delete_calls) == 3
        serial_urls = [c[1] for c in delete_calls[1:]]
        assert any("10" in u for u in serial_urls)
        assert any("11" in u for u in serial_urls)

    def test_non_404_http_error_propagates(self, client, fake_http, fake_response):
        """A 500 from the bulk endpoint must not be silently swallowed."""
        from mediaman.services.http_client import SafeHTTPError
        fake_http.queue("GET", fake_response(json_data=[{"id": 99, "seasonNumber": 2}]))
        fake_http.queue("DELETE", fake_response(status=500, text="server error"))

        with pytest.raises(SafeHTTPError):
            client.delete_episode_files(series_id=1, season_number=2)

    def test_no_matching_files_sends_no_delete(self, client, fake_http, fake_response):
        """If no episode files match the season, DELETE is never called."""
        fake_http.queue("GET", fake_response(json_data=[
            {"id": 1, "seasonNumber": 2},  # wrong season
        ]))

        client.delete_episode_files(series_id=3, season_number=1)

        assert _calls(fake_http, "DELETE") == []


class TestGetMissingSeries:
    def test_dedupes_by_series_id_across_pages(self, client, fake_http, fake_response):
        page_one = {
            "records": [
                {"series": {"id": 1, "title": "A"}},
                {"series": {"id": 2, "title": "B"}},
                {"series": {"id": 1, "title": "A"}},
            ],
            "totalRecords": 500,
        }
        page_two = {
            "records": [
                {"series": {"id": 3, "title": "C"}},
            ],
            "totalRecords": 500,
        }
        fake_http.queue("GET", fake_response(json_data=page_one))
        fake_http.queue("GET", fake_response(json_data=page_two))

        out = client.get_missing_series()

        assert out == {1: "A", 2: "B", 3: "C"}

    def test_empty_response_short_circuits(self, client, fake_http, fake_response):
        fake_http.queue("GET", fake_response(json_data={"records": [], "totalRecords": 0}))
        assert client.get_missing_series() == {}
        assert len(_calls(fake_http, "GET")) == 1
