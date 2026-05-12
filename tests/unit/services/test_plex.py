"""Tests for Plex service client."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests as http_requests

from mediaman.services.infra.http import SafeHTTPError
from mediaman.services.infra.url_safety import SSRFRefused
from mediaman.services.media_meta import plex as plex_module
from mediaman.services.media_meta.plex import (
    PlexClient,
    _SafePlexSession,
    _scrub_plex_token,
)
from tests.helpers.factories import make_plex_episode, make_plex_season, make_plex_show


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
    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_connect(self, mock_cls):
        mock_cls.return_value = MagicMock()
        client = PlexClient("http://plex:32400", "test-token")
        assert client.server is not None

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_libraries(self, mock_cls, mock_server):
        mock_cls.return_value = mock_server
        client = PlexClient("http://plex:32400", "test-token")
        libs = client.get_libraries()
        assert len(libs) == 1
        assert libs[0]["id"] == "1"
        assert libs[0]["type"] == "movie"
        assert libs[0]["title"] == "Movies"

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_movie_items(self, mock_cls, mock_server):
        movie = MagicMock()
        movie.ratingKey = 123
        movie.title = "Test Movie"
        movie.addedAt = datetime(2026, 1, 1, tzinfo=UTC)
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

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_movie_items_file_details(self, mock_cls, mock_server):
        movie = MagicMock()
        movie.ratingKey = 42
        movie.title = "Another Film"
        movie.addedAt = datetime(2026, 2, 1, tzinfo=UTC)
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

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_watch_history(self, mock_cls, fake_http, fake_response):
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        body = b'<MediaContainer><Video viewedAt="1740700800" accountID="1"/></MediaContainer>'
        fake_http.queue(
            "GET", fake_response(content=body, headers={"Content-Length": str(len(body))})
        )

        client = PlexClient("http://plex:32400", "test-token")
        history = client.get_watch_history("123")
        assert len(history) == 1
        assert history[0]["account_id"] == 1
        assert len(fake_http.calls) == 1

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_watch_history_viewed_at(self, mock_cls, fake_http, fake_response):
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        body = b'<MediaContainer><Video viewedAt="1772150400" accountID="2"/></MediaContainer>'
        fake_http.queue(
            "GET", fake_response(content=body, headers={"Content-Length": str(len(body))})
        )

        client = PlexClient("http://plex:32400", "test-token")
        history = client.get_watch_history("456")
        assert history[0]["viewed_at"].year == 2026
        assert history[0]["account_id"] == 2

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_watch_history_rejects_over_cap(self, mock_cls, fake_http, fake_response):
        """Responses exceeding the 4 MiB cap must raise, not be parsed."""
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        oversized = 5 * 1024 * 1024
        fake_http.queue(
            "GET",
            fake_response(
                content=b"",
                headers={"Content-Length": str(oversized)},
            ),
        )

        client = PlexClient("http://plex:32400", "test-token")
        with pytest.raises(ValueError):
            client.get_watch_history("123")

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_watch_history_raises_for_http_error(self, mock_cls, fake_http, fake_response):
        """HTTP errors must propagate — don't silently parse a 500 body."""
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        fake_http.queue("GET", fake_response(status=500, text="boom"))

        client = PlexClient("http://plex:32400", "test-token")
        with pytest.raises(http_requests.HTTPError):
            client.get_watch_history("123")

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_watch_history_streaming_exceeds_cap(self, mock_cls, fake_http, fake_response):
        """A server that under-declares Content-Length is still capped mid-stream."""
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        chunks = [b"A" * 1024 * 1024] * 5
        resp = fake_response(content=b"".join(chunks), headers={})
        # Override iter_content to return the chunks one by one so the
        # size-cap enforcement kicks in mid-stream.
        resp.iter_content = lambda chunk_size=65536: iter(chunks)
        fake_http.queue("GET", resp)

        client = PlexClient("http://plex:32400", "test-token")
        with pytest.raises(ValueError):
            client.get_watch_history("123")

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_show_seasons_skips_season_zero(self, mock_cls):
        mock_server = MagicMock()
        mock_cls.return_value = mock_server

        ep = make_plex_episode(title="Pilot")
        special_season = make_plex_season(index=0, rating_key=999)
        real_season = make_plex_season(index=1, rating_key=200, episodes=[ep])

        show = make_plex_show(seasons=[special_season, real_season])

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

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_show_seasons_fallback_added_at(self, mock_cls):
        """When season.addedAt is None, use earliest episode addedAt."""
        mock_server = MagicMock()
        mock_cls.return_value = mock_server

        season = MagicMock()
        season.index = 1
        season.ratingKey = 300
        season.addedAt = None  # force fallback

        ep1 = MagicMock()
        ep1.addedAt = datetime(2026, 2, 1, tzinfo=UTC)
        ep1.media = []
        ep1.history.return_value = []

        ep2 = MagicMock()
        ep2.addedAt = datetime(2026, 1, 5, tzinfo=UTC)  # earlier
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

        assert results[0]["added_at"] == datetime(2026, 1, 5, tzinfo=UTC)

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_season_watch_history(self, mock_cls, fake_http, fake_response):
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

        body = b'<MediaContainer><Video viewedAt="1773244800" accountID="3"/></MediaContainer>'
        fake_http.queue(
            "GET", fake_response(content=body, headers={"Content-Length": str(len(body))})
        )

        client = PlexClient("http://plex:32400", "test-token")
        history = client.get_season_watch_history("500")

        assert len(history) == 1
        assert history[0]["account_id"] == 3
        assert history[0]["episode_title"] == "Episode 1"

    @patch("mediaman.services.media_meta.plex.PlexServer")
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

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_get_watch_history_uses_params_kwarg(self, mock_cls, fake_http, fake_response):
        """H53: metadataItemID must be sent via params= not interpolated into the URL."""
        mock_server = MagicMock()
        mock_server._baseurl = "http://plex:32400"
        mock_server._token = "test-token"
        mock_cls.return_value = mock_server

        body = b"<MediaContainer></MediaContainer>"
        fake_http.queue(
            "GET", fake_response(content=body, headers={"Content-Length": str(len(body))})
        )

        client = PlexClient("http://plex:32400", "test-token")
        client.get_watch_history("123")

        assert len(fake_http.calls) == 1
        _, url, kwargs = fake_http.calls[0]
        # The rating_key must appear in params, not the raw URL.
        assert "params" in kwargs
        assert kwargs["params"]["metadataItemID"] == "123"
        assert "metadataItemID" not in url


class TestScrubPlexToken:
    """H52: ``_scrub_plex_token`` must redact token values from strings."""

    def test_scrubs_token_in_query_string(self):
        msg = "http://plex:32400/status?X-Plex-Token=supersecret&sort=desc"
        result = _scrub_plex_token(msg)
        assert "supersecret" not in result
        assert "X-Plex-Token=<redacted>" in result

    def test_scrubs_token_case_insensitive(self):
        msg = "x-plex-token=MySecret123"
        result = _scrub_plex_token(msg)
        assert "MySecret123" not in result
        assert "<redacted>" in result

    def test_no_token_unchanged(self):
        msg = "http://plex:32400/status?sort=desc"
        assert _scrub_plex_token(msg) == msg

    def test_token_at_end_of_string_scrubbed(self):
        msg = "error from X-Plex-Token=abc123"
        result = _scrub_plex_token(msg)
        assert "abc123" not in result
        assert "<redacted>" in result

    def test_token_before_ampersand_scrubbed(self):
        msg = "url?X-Plex-Token=tok&other=val"
        result = _scrub_plex_token(msg)
        assert "tok" not in result
        assert "other=val" in result


class TestPlexClientUrlValidation:
    """The configured Plex URL must be validated by the SSRF guard at
    PlexClient construction, not just when the operator first saved it.

    A URL that resolved cleanly when stored could have started pointing
    at a metadata endpoint (or had its DNS rebound) by the time the
    next scan runs. Re-validating at use-time catches that.
    """

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_construction_revalidates_url(self, mock_cls, monkeypatch):
        """A URL that fails the SSRF guard must be refused at __init__."""
        mock_cls.return_value = MagicMock()
        # Force the SSRF guard to refuse.
        monkeypatch.setattr(
            plex_module,
            "resolve_safe_outbound_url",
            lambda url: (False, None, None),
        )
        with pytest.raises(SSRFRefused):
            PlexClient("http://malicious.example/", "token")
        # PlexServer was never called — the refusal happened before any
        # token-bearing request could go out.
        mock_cls.assert_not_called()

    @patch("mediaman.services.media_meta.plex.PlexServer")
    def test_construction_passes_safe_session(self, mock_cls, monkeypatch):
        """PlexServer must be constructed with our hardened session.

        Without this, plexapi falls back to a vanilla requests.Session
        — no SSRF re-check, no redirect refusal, no body cap.
        """
        captured: dict = {}

        def fake_plexserver(url, token, *, session=None, timeout=None):
            captured["session"] = session
            return MagicMock()

        mock_cls.side_effect = fake_plexserver
        monkeypatch.setattr(
            plex_module,
            "resolve_safe_outbound_url",
            lambda url: (True, "host.example", "203.0.113.1"),
        )
        client = PlexClient("http://host.example/", "token")
        assert isinstance(captured["session"], _SafePlexSession)
        # PlexClient holds a reference too, for visibility from tests.
        assert client._safe_session is captured["session"]


class TestSafePlexSession:
    """Direct tests of the hardened ``requests.Session`` injected into
    plexapi. Each behaviour pin matches a hardening property of
    SafeHTTPClient — SSRF re-check, redirect refusal, body cap, timeout
    normalisation, DNS pinning."""

    def _stub_response(self, *, status=200, body=b"", headers=None):
        resp = MagicMock(spec=http_requests.Response)
        resp.status_code = status
        resp.headers = headers or {}
        resp.iter_content = lambda chunk_size=65536: iter([body])
        resp.close = MagicMock()
        return resp

    def test_refuses_unsafe_url(self, monkeypatch):
        """A URL that fails the SSRF guard must raise SafeHTTPError
        before any HTTP attempt."""
        monkeypatch.setattr(
            plex_module,
            "resolve_safe_outbound_url",
            lambda url: (False, None, None),
        )
        called: list = []

        def fake_super_request(self, method, url, **kwargs):
            called.append((method, url))
            return self._stub_response()

        monkeypatch.setattr(http_requests.Session, "request", fake_super_request, raising=True)
        sess = _SafePlexSession()
        with pytest.raises(SafeHTTPError) as excinfo:
            sess.get("http://blocked.example/")
        assert "SSRF guard" in excinfo.value.body_snippet
        # No transport call was made.
        assert called == []

    def test_forces_no_redirects(self, monkeypatch):
        """``allow_redirects`` must be False on every Plex call.

        A 302 to ``169.254.169.254`` would otherwise leak the
        X-Plex-Token into cloud metadata.
        """
        monkeypatch.setattr(
            plex_module,
            "resolve_safe_outbound_url",
            lambda url: (True, "h.example", "203.0.113.1"),
        )
        captured: dict = {}

        def fake_super_request(self_, method, url, **kwargs):
            captured.update(kwargs)
            resp = MagicMock(spec=http_requests.Response)
            resp.status_code = 200
            resp.headers = {}
            resp.iter_content = lambda chunk_size=65536: iter([b"<x/>"])
            resp.close = MagicMock()
            return resp

        monkeypatch.setattr(http_requests.Session, "request", fake_super_request)
        # Pretend a caller passed allow_redirects=True — we must override.
        sess = _SafePlexSession()
        sess.get("http://h.example/", allow_redirects=True)
        assert captured["allow_redirects"] is False

    def test_caps_oversize_response_via_content_length(self, monkeypatch):
        """A response with Content-Length > 16 MiB must be rejected."""
        monkeypatch.setattr(
            plex_module,
            "resolve_safe_outbound_url",
            lambda url: (True, "h.example", "203.0.113.1"),
        )

        def fake_super_request(self_, method, url, **kwargs):
            resp = MagicMock(spec=http_requests.Response)
            resp.status_code = 200
            resp.headers = {"Content-Length": str(20 * 1024 * 1024)}
            resp.iter_content = lambda chunk_size=65536: iter([b""])
            resp.close = MagicMock()
            return resp

        monkeypatch.setattr(http_requests.Session, "request", fake_super_request)
        with pytest.raises(SafeHTTPError):
            _SafePlexSession().get("http://h.example/large")

    def test_caps_streamed_oversize_response(self, monkeypatch):
        """A server lying about / omitting Content-Length is still capped."""
        monkeypatch.setattr(
            plex_module,
            "resolve_safe_outbound_url",
            lambda url: (True, "h.example", "203.0.113.1"),
        )
        chunks = [b"A" * 1024 * 1024 for _ in range(20)]  # 20 MiB total

        def fake_super_request(self_, method, url, **kwargs):
            resp = MagicMock(spec=http_requests.Response)
            resp.status_code = 200
            resp.headers = {}
            resp.iter_content = lambda chunk_size=65536: iter(chunks)
            resp.close = MagicMock()
            return resp

        monkeypatch.setattr(http_requests.Session, "request", fake_super_request)
        with pytest.raises(SafeHTTPError):
            _SafePlexSession().get("http://h.example/streamed")

    def test_normalises_int_timeout_to_tuple(self, monkeypatch):
        """plexapi often passes a single int timeout — we coerce to (5, 30)."""
        monkeypatch.setattr(
            plex_module,
            "resolve_safe_outbound_url",
            lambda url: (True, "h.example", "203.0.113.1"),
        )
        captured: dict = {}

        def fake_super_request(self_, method, url, **kwargs):
            captured.update(kwargs)
            resp = MagicMock(spec=http_requests.Response)
            resp.status_code = 200
            resp.headers = {}
            resp.iter_content = lambda chunk_size=65536: iter([b"{}"])
            resp.close = MagicMock()
            return resp

        monkeypatch.setattr(http_requests.Session, "request", fake_super_request)
        # plexapi default — single int.
        _SafePlexSession().get("http://h.example/", timeout=30)
        assert captured["timeout"] == (5.0, 30.0)

    def test_honours_caller_supplied_tuple_timeout(self, monkeypatch):
        """If the caller already passed a (connect, read) tuple, keep it."""
        monkeypatch.setattr(
            plex_module,
            "resolve_safe_outbound_url",
            lambda url: (True, "h.example", "203.0.113.1"),
        )
        captured: dict = {}

        def fake_super_request(self_, method, url, **kwargs):
            captured.update(kwargs)
            resp = MagicMock(spec=http_requests.Response)
            resp.status_code = 200
            resp.headers = {}
            resp.iter_content = lambda chunk_size=65536: iter([b"{}"])
            resp.close = MagicMock()
            return resp

        monkeypatch.setattr(http_requests.Session, "request", fake_super_request)
        _SafePlexSession().get("http://h.example/", timeout=(2.0, 7.5))
        assert captured["timeout"] == (2.0, 7.5)

    def test_pins_dns_during_dispatch(self, monkeypatch):
        """The validated IP must be pinned for the duration of the request."""
        import socket as _socket

        from mediaman.services.infra.http import dns_pinning as _dns_pinning

        monkeypatch.setattr(
            plex_module,
            "resolve_safe_outbound_url",
            lambda url: (True, "pinme.example", "203.0.113.42"),
        )
        # Ensure the global pin hook is active.
        monkeypatch.setattr(_socket, "getaddrinfo", _dns_pinning._patched_getaddrinfo)

        captured: dict = {}

        def fake_super_request(self_, method, url, **kwargs):
            captured["pin_lookup"] = _socket.getaddrinfo("pinme.example", 0)
            resp = MagicMock(spec=http_requests.Response)
            resp.status_code = 200
            resp.headers = {}
            resp.iter_content = lambda chunk_size=65536: iter([b"<x/>"])
            resp.close = MagicMock()
            return resp

        monkeypatch.setattr(http_requests.Session, "request", fake_super_request)
        _SafePlexSession().get("http://pinme.example/")
        assert captured["pin_lookup"][0][4][0] == "203.0.113.42"

    def test_scrubs_token_from_unsafe_url_error(self, monkeypatch):
        """When the URL contains a token, the SSRF refusal message must
        not echo it back."""
        monkeypatch.setattr(
            plex_module,
            "resolve_safe_outbound_url",
            lambda url: (False, None, None),
        )
        with pytest.raises(SafeHTTPError) as excinfo:
            _SafePlexSession().get("http://blocked.example/path?X-Plex-Token=secret-tok-123")
        assert "secret-tok-123" not in excinfo.value.url
        assert "<redacted>" in excinfo.value.url
