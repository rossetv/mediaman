"""Tests for Plex service client."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests as http_requests

from mediaman.services.plex import PlexClient


@pytest.fixture
def mock_server():
    server = MagicMock()
    section = MagicMock()
    section.key = "1"
    section.type = "movie"
    section.title = "Movies"
    server.library.sections.return_value = [section]
    return server


class TestPlexClient:
    @patch("mediaman.services.plex.PlexServer")
    def test_connect(self, mock_cls):
        mock_cls.return_value = MagicMock()
        client = PlexClient("http://plex:32400", "test-token")
        assert client.server is not None

    @patch("mediaman.services.plex.PlexServer")
    def test_get_libraries(self, mock_cls, mock_server):
        mock_cls.return_value = mock_server
        client = PlexClient("http://plex:32400", "test-token")
        libs = client.get_libraries()
        assert len(libs) == 1
        assert libs[0]["id"] == "1"
        assert libs[0]["type"] == "movie"
        assert libs[0]["title"] == "Movies"

    @patch("mediaman.services.plex.PlexServer")
    def test_get_movie_items(self, mock_cls, mock_server):
        movie = MagicMock()
        movie.ratingKey = 123
        movie.title = "Test Movie"
        movie.addedAt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        movie.media = [MagicMock()]
        movie.media[0].parts = [MagicMock()]
        movie.media[0].parts[0].file = "/data/movies/Test Movie (2024)/movie.mkv"
        movie.media[0].parts[0].size = 10_000_000_000
        movie.thumb = "/library/metadata/123/thumb/999"
        section = MagicMock()
        section.key = "1"
        section.all.return_value = [movie]
        mock_server.library.sectionByID.return_value = section
        mock_cls.return_value = mock_server

        client = PlexClient("http://plex:32400", "test-token")
        items = client.get_movie_items("1")
        assert len(items) == 1
        assert items[0]["plex_rating_key"] == "123"
        assert items[0]["title"] == "Test Movie"

    @patch("mediaman.services.plex.PlexServer")
    def test_get_movie_items_file_details(self, mock_cls, mock_server):
        movie = MagicMock()
        movie.ratingKey = 42
        movie.title = "Another Film"
        movie.addedAt = datetime(2026, 2, 1, tzinfo=timezone.utc)
        movie.media = [MagicMock()]
        movie.media[0].parts = [MagicMock()]
        movie.media[0].parts[0].file = "/data/movies/Another Film/film.mkv"
        movie.media[0].parts[0].size = 5_000_000_000
        movie.thumb = "/library/metadata/42/thumb/1"
        section = MagicMock()
        section.all.return_value = [movie]
        mock_server.library.sectionByID.return_value = section
        mock_cls.return_value = mock_server

        client = PlexClient("http://plex:32400", "test-token")
        items = client.get_movie_items("1")
        assert items[0]["file_path"] == "/data/movies/Another Film/film.mkv"
        assert items[0]["file_size_bytes"] == 5_000_000_000
        assert items[0]["poster_path"] == "/library/metadata/42/thumb/1"

    @patch("mediaman.services.plex.http_requests.get")
    @patch("mediaman.services.plex.PlexServer")
    def test_get_watch_history(self, mock_cls, mock_http_get):
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        # Mock the streamed Plex API XML response.
        body = b'<MediaContainer><Video viewedAt="1740700800" accountID="1"/></MediaContainer>'
        resp = MagicMock()
        resp.headers = {"Content-Length": str(len(body))}
        resp.iter_content.return_value = iter([body])
        mock_http_get.return_value = resp

        client = PlexClient("http://plex:32400", "test-token")
        history = client.get_watch_history("123")
        assert len(history) == 1
        assert history[0]["account_id"] == 1
        # Streaming path must be used — cheaper than reading .text twice.
        mock_http_get.assert_called_once()
        _, kwargs = mock_http_get.call_args
        assert kwargs.get("stream") is True

    @patch("mediaman.services.plex.http_requests.get")
    @patch("mediaman.services.plex.PlexServer")
    def test_get_watch_history_viewed_at(self, mock_cls, mock_http_get):
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        # 2026-03-01 00:00:00 UTC = 1772150400
        body = b'<MediaContainer><Video viewedAt="1772150400" accountID="2"/></MediaContainer>'
        resp = MagicMock()
        resp.headers = {"Content-Length": str(len(body))}
        resp.iter_content.return_value = iter([body])
        mock_http_get.return_value = resp

        client = PlexClient("http://plex:32400", "test-token")
        history = client.get_watch_history("456")
        assert history[0]["viewed_at"].year == 2026
        assert history[0]["account_id"] == 2

    @patch("mediaman.services.plex.http_requests.get")
    @patch("mediaman.services.plex.PlexServer")
    def test_get_watch_history_rejects_over_cap(self, mock_cls, mock_http_get):
        """Responses exceeding the 4 MiB cap must raise, not be parsed."""
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        # 5 MiB declared — must be refused before any parsing happens.
        oversized = 5 * 1024 * 1024
        resp = MagicMock()
        resp.headers = {"Content-Length": str(oversized)}
        resp.iter_content.return_value = iter([b""])
        mock_http_get.return_value = resp

        client = PlexClient("http://plex:32400", "test-token")
        with pytest.raises(ValueError, match="too large"):
            client.get_watch_history("123")

    @patch("mediaman.services.plex.http_requests.get")
    @patch("mediaman.services.plex.PlexServer")
    def test_get_watch_history_raises_for_http_error(self, mock_cls, mock_http_get):
        """HTTP errors must propagate — don't silently parse a 500 body."""
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        resp = MagicMock()
        resp.raise_for_status.side_effect = http_requests.HTTPError("500 server error")
        mock_http_get.return_value = resp

        client = PlexClient("http://plex:32400", "test-token")
        with pytest.raises(http_requests.HTTPError):
            client.get_watch_history("123")

    @patch("mediaman.services.plex.http_requests.get")
    @patch("mediaman.services.plex.PlexServer")
    def test_get_watch_history_streaming_exceeds_cap(self, mock_cls, mock_http_get):
        """A server that under-declares Content-Length is still capped mid-stream."""
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        chunks = [b"A" * 1024 * 1024] * 5  # 5 MiB total via chunks
        resp = MagicMock()
        resp.headers = {}  # no Content-Length — forces streaming check
        resp.iter_content.return_value = iter(chunks)
        mock_http_get.return_value = resp

        client = PlexClient("http://plex:32400", "test-token")
        with pytest.raises(ValueError, match="exceeded size cap"):
            client.get_watch_history("123")

    @patch("mediaman.services.plex.PlexServer")
    def test_get_show_seasons_skips_season_zero(self, mock_cls):
        mock_server = MagicMock()
        mock_cls.return_value = mock_server

        special_season = MagicMock()
        special_season.index = 0

        real_season = MagicMock()
        real_season.index = 1
        real_season.ratingKey = 200
        real_season.addedAt = datetime(2026, 1, 15, tzinfo=timezone.utc)

        ep = MagicMock()
        ep.addedAt = datetime(2026, 1, 10, tzinfo=timezone.utc)
        ep.title = "Pilot"
        ep.media = [MagicMock()]
        ep.media[0].parts = [MagicMock()]
        ep.media[0].parts[0].file = "/data/tv/Show/Season 1/ep01.mkv"
        ep.media[0].parts[0].size = 2_000_000_000
        ep.history.return_value = []
        real_season.episodes.return_value = [ep]

        show = MagicMock()
        show.ratingKey = 100
        show.title = "Test Show"
        show.thumb = "/library/metadata/100/thumb/1"
        show.seasons.return_value = [special_season, real_season]

        section = MagicMock()
        section.all.return_value = [show]
        mock_server.library.sectionByID.return_value = section

        client = PlexClient("http://plex:32400", "test-token")
        results = client.get_show_seasons("2")

        assert len(results) == 1
        assert results[0]["season_number"] == 1
        assert results[0]["plex_rating_key"] == "200"
        assert results[0]["show_title"] == "Test Show"
        assert results[0]["episode_count"] == 1
        assert results[0]["file_path"] == "/data/tv/Show/Season 1"
        assert results[0]["file_size_bytes"] == 2_000_000_000
        assert results[0]["show_rating_key"] == "100"

    @patch("mediaman.services.plex.PlexServer")
    def test_get_show_seasons_fallback_added_at(self, mock_cls):
        """When season.addedAt is None, use earliest episode addedAt."""
        mock_server = MagicMock()
        mock_cls.return_value = mock_server

        season = MagicMock()
        season.index = 1
        season.ratingKey = 300
        season.addedAt = None  # force fallback

        ep1 = MagicMock()
        ep1.addedAt = datetime(2026, 2, 1, tzinfo=timezone.utc)
        ep1.media = []
        ep1.history.return_value = []

        ep2 = MagicMock()
        ep2.addedAt = datetime(2026, 1, 5, tzinfo=timezone.utc)  # earlier
        ep2.media = []
        ep2.history.return_value = []

        season.episodes.return_value = [ep1, ep2]

        show = MagicMock()
        show.ratingKey = 50
        show.title = "Fallback Show"
        show.thumb = "/thumb/50"
        show.seasons.return_value = [season]

        section = MagicMock()
        section.all.return_value = [show]
        mock_server.library.sectionByID.return_value = section

        client = PlexClient("http://plex:32400", "test-token")
        results = client.get_show_seasons("3")

        assert results[0]["added_at"] == datetime(2026, 1, 5, tzinfo=timezone.utc)

    @patch("mediaman.services.plex.PlexServer")
    @patch("mediaman.services.plex.http_requests.get")
    def test_get_season_watch_history(self, mock_http_get, mock_cls):
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        ep = MagicMock()
        ep.title = "Episode 1"
        ep.ratingKey = 501

        season = MagicMock()
        season.episodes.return_value = [ep]
        mock_server.fetchItem.return_value = season

        # Mock the streamed API response for the episode's history
        body = b'<MediaContainer><Video viewedAt="1773244800" accountID="3"/></MediaContainer>'
        resp = MagicMock()
        resp.headers = {"Content-Length": str(len(body))}
        resp.iter_content.return_value = iter([body])
        mock_http_get.return_value = resp

        client = PlexClient("http://plex:32400", "test-token")
        history = client.get_season_watch_history("500")

        assert len(history) == 1
        assert history[0]["account_id"] == 3
        assert history[0]["episode_title"] == "Episode 1"

    @patch("mediaman.services.plex.PlexServer")
    def test_get_accounts(self, mock_cls):
        import xml.etree.ElementTree as ET

        mock_server = MagicMock()
        mock_cls.return_value = mock_server

        xml_str = """<MediaContainer>
            <Account id="1" name="" />
            <Account id="2" name="Alice" />
            <Account id="3" name="Bob" />
        </MediaContainer>"""
        mock_server.query.return_value = ET.fromstring(xml_str)

        client = PlexClient("http://plex:32400", "test-token")
        accounts = client.get_accounts()

        assert len(accounts) == 2
        assert accounts[0] == {"id": 2, "name": "Alice"}
        assert accounts[1] == {"id": 3, "name": "Bob"}
        mock_server.query.assert_called_once_with("/accounts")
