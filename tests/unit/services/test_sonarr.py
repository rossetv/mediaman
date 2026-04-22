"""Tests for Sonarr API client."""
from unittest.mock import patch, MagicMock
import pytest
from mediaman.services.sonarr import SonarrClient


@pytest.fixture
def client():
    return SonarrClient("http://sonarr:8989", "test-api-key")


class TestSonarrClient:
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_get_series(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [
            {"id": 1, "title": "Modern Family", "path": "/tv/Modern Family", "seasons": [{"seasonNumber": 1, "monitored": True}]},
        ])
        series = client.get_series()
        assert len(series) == 1
        assert series[0]["title"] == "Modern Family"

    @patch("mediaman.services.arr_client_base.requests.put")
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_unmonitor_season(self, mock_get, mock_put, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {
            "id": 1, "title": "Modern Family",
            "seasons": [{"seasonNumber": 1, "monitored": True}, {"seasonNumber": 2, "monitored": True}],
        })
        mock_put.return_value = MagicMock(status_code=202)
        client.unmonitor_season(series_id=1, season_number=2)
        put_data = mock_put.call_args[1]["json"]
        season_2 = next(s for s in put_data["seasons"] if s["seasonNumber"] == 2)
        assert season_2["monitored"] is False

    @patch("mediaman.services.sonarr.requests.post")
    @patch("mediaman.services.arr_client_base.requests.put")
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_remonitor_season(self, mock_get, mock_put, mock_post, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {
            "id": 1, "title": "Test", "seasons": [{"seasonNumber": 1, "monitored": False}],
        })
        mock_put.return_value = MagicMock(status_code=202)
        mock_post.return_value = MagicMock(status_code=201)
        client.remonitor_season(series_id=1, season_number=1)
        put_data = mock_put.call_args[1]["json"]
        assert put_data["seasons"][0]["monitored"] is True
        mock_post.assert_called_once()

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_test_connection(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"version": "4.0"})
        assert client.test_connection() is True

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_test_connection_failure(self, mock_get, client):
        mock_get.side_effect = Exception("Connection refused")
        assert client.test_connection() is False


def test_search_series_posts_seriessearch_command(monkeypatch):
    """search_series issues POST /api/v3/command with SeriesSearch payload."""
    from mediaman.services.sonarr import SonarrClient

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json

        class R:
            def raise_for_status(self):
                return None

        return R()

    monkeypatch.setattr("mediaman.services.sonarr.requests.post", fake_post)

    client = SonarrClient("https://sonarr.local", "key456")
    client.search_series(7)

    assert captured["url"] == "https://sonarr.local/api/v3/command"
    assert captured["headers"] == {"X-Api-Key": "key456"}
    assert captured["json"] == {"name": "SeriesSearch", "seriesId": 7}


def test_get_episodes_returns_episode_list(monkeypatch):
    """get_episodes fetches /api/v3/episode?seriesId=<id> and returns the list."""
    from mediaman.services.sonarr import SonarrClient

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url

        class R:
            def raise_for_status(self):
                return None

            def json(self):
                return [{"id": 1, "airDateUtc": "2099-01-01T00:00:00Z"}]

        return R()

    monkeypatch.setattr("mediaman.services.arr_client_base.requests.get", fake_get)

    client = SonarrClient("https://sonarr.local", "key456")
    result = client.get_episodes(42)

    assert captured["url"] == "https://sonarr.local/api/v3/episode?seriesId=42"
    assert isinstance(result, list)
    assert result[0]["id"] == 1


def test_get_episodes_returns_empty_list_on_non_list_response(monkeypatch):
    """get_episodes returns [] when the API returns a non-list (e.g. error envelope)."""
    from mediaman.services.sonarr import SonarrClient

    def fake_get(url, headers=None, timeout=None):
        class R:
            def raise_for_status(self):
                return None

            def json(self):
                return {"error": "unexpected"}

        return R()

    monkeypatch.setattr("mediaman.services.arr_client_base.requests.get", fake_get)
    client = SonarrClient("https://sonarr.local", "key456")
    result = client.get_episodes(1)
    assert result == []


class TestLookupSeriesByTmdb:
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_returns_first_hit(self, mock_get, client):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"tvdbId": 101, "title": "Arcane"},
                {"tvdbId": 102, "title": "Other"},
            ],
        )
        result = client.lookup_series_by_tmdb(12345)
        assert result["tvdbId"] == 101
        called_url = mock_get.call_args[0][0]
        assert "term=tmdb:12345" in called_url

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_returns_none_on_empty(self, mock_get, client):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [])
        assert client.lookup_series_by_tmdb(12345) is None

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_returns_none_on_exception(self, mock_get, client):
        mock_get.side_effect = Exception("boom")
        assert client.lookup_series_by_tmdb(12345) is None


class TestAddSeriesWithSeasons:
    @patch("mediaman.services.sonarr.requests.post")
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_sends_correct_monitored_flags(self, mock_get, mock_post, client):
        # Sonarr returns root folders, lookup, then the add response.
        lookup_result = [{
            "tvdbId": 999,
            "title": "Breaking Bad",
            "seasons": [
                {"seasonNumber": 0}, {"seasonNumber": 1}, {"seasonNumber": 2},
                {"seasonNumber": 3}, {"seasonNumber": 4}, {"seasonNumber": 5},
            ],
        }]
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: [{"path": "/tv"}]),
            MagicMock(status_code=200, json=lambda: lookup_result),
        ]
        mock_post.return_value = MagicMock(status_code=201, json=lambda: {"id": 42})

        client.add_series_with_seasons(
            tvdb_id=999, title="Breaking Bad",
            monitored_seasons=[1, 2], search_seasons=[2],
        )

        # First POST is the series add.
        add_call = mock_post.call_args_list[0]
        body = add_call[1]["json"]
        assert body["addOptions"]["searchForMissingEpisodes"] is False
        by_num = {s["seasonNumber"]: s["monitored"] for s in body["seasons"]}
        assert by_num == {0: False, 1: True, 2: True, 3: False, 4: False, 5: False}

        # Then one SeasonSearch command per entry in search_seasons.
        search_calls = [
            c for c in mock_post.call_args_list
            if "/api/v3/command" in c[0][0]
        ]
        assert len(search_calls) == 1
        assert search_calls[0][1]["json"] == {
            "name": "SeasonSearch", "seriesId": 42, "seasonNumber": 2,
        }

    @patch("mediaman.services.sonarr.requests.post")
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_no_search_when_search_seasons_empty(self, mock_get, mock_post, client):
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: [{"path": "/tv"}]),
            MagicMock(status_code=200, json=lambda: [{
                "tvdbId": 10, "title": "X",
                "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
            }]),
        ]
        mock_post.return_value = MagicMock(status_code=201, json=lambda: {"id": 7})

        client.add_series_with_seasons(
            tvdb_id=10, title="X",
            monitored_seasons=[1, 2], search_seasons=[],
        )

        command_calls = [
            c for c in mock_post.call_args_list
            if "/api/v3/command" in c[0][0]
        ]
        assert command_calls == []


class TestGetMissingSeries:
    @patch("mediaman.services.arr_client_base.requests.get")
    def test_dedupes_by_series_id_across_pages(self, mock_get, client):
        page_one = {
            "records": [
                {"series": {"id": 1, "title": "A"}},
                {"series": {"id": 2, "title": "B"}},
                {"series": {"id": 1, "title": "A"}},  # second missing ep of A
            ],
            "totalRecords": 4,
        }
        page_two = {
            "records": [
                {"series": {"id": 3, "title": "C"}},
            ],
            "totalRecords": 4,
        }
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: page_one),
            MagicMock(status_code=200, json=lambda: page_two),
        ]
        # Force a small effective page size by monkey-patching after call,
        # or just trust the hard cap — easier to drive via totalRecords.
        # We rely on the client's own pagination: totalRecords=4 with
        # pageSize=250 would stop after page 1, so bump totalRecords to
        # force a second page.
        page_one["totalRecords"] = 500
        page_two["totalRecords"] = 500

        out = client.get_missing_series()

        assert out == {1: "A", 2: "B", 3: "C"}

    @patch("mediaman.services.arr_client_base.requests.get")
    def test_empty_response_short_circuits(self, mock_get, client):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"records": [], "totalRecords": 0},
        )
        assert client.get_missing_series() == {}
        assert mock_get.call_count == 1
