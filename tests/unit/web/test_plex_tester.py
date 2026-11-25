"""Tests for the test_plex tester structured error response.

Verifies that test_plex returns a structured JSONResponse on transport and
auth failures rather than raising unhandled exceptions.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests
from plexapi.exceptions import BadRequest, NotFound, Unauthorized

from mediaman.services.infra import SafeHTTPError
from mediaman.web.routes.settings.testers import test_plex as _test_plex


class TestTestPlexStructuredResponse:
    """test_plex must return a structured response on every failure path."""

    def _settings(self, url: str = "http://plex:32400", token: str = "abc") -> dict:
        return {"plex_url": url, "plex_token": token}

    def test_missing_url_returns_error(self):
        resp = _test_plex({"plex_url": "", "plex_token": "token"})
        data = resp.body
        import json

        body = json.loads(data)
        assert body["ok"] is False
        assert "required" in body["error"].lower()

    def test_missing_token_returns_error(self):
        resp = _test_plex({"plex_url": "http://plex:32400", "plex_token": ""})
        import json

        body = json.loads(resp.body)
        assert body["ok"] is False

    def test_safe_http_error_401_returns_auth_failed(self):
        # test_plex imports PlexClient lazily from mediaman.services.media_meta.plex,
        # so the patch must target that module's namespace, not the testers module.
        with patch(
            "mediaman.services.media_meta.plex.PlexClient",
            side_effect=SafeHTTPError(401, "Unauthorized", b""),
        ):
            resp = _test_plex(self._settings())
        import json

        body = json.loads(resp.body)
        assert body["ok"] is False
        assert body["error"] == "auth_failed"

    def test_requests_connection_error_returns_connection_refused(self):
        """requests.ConnectionError must return a structured error, not propagate."""
        with patch(
            "mediaman.services.media_meta.plex.PlexClient",
            side_effect=requests.ConnectionError("Connection refused"),
        ):
            resp = _test_plex(self._settings())
        import json

        body = json.loads(resp.body)
        assert body["ok"] is False
        assert body["error"] in ("connection_refused", "timeout")

    def test_requests_timeout_returns_timeout(self):
        """requests.Timeout must return error=timeout, not propagate."""
        with patch(
            "mediaman.services.media_meta.plex.PlexClient",
            side_effect=requests.Timeout("timed out"),
        ):
            resp = _test_plex(self._settings())
        import json

        body = json.loads(resp.body)
        assert body["ok"] is False
        assert body["error"] == "timeout"

    def test_successful_connection_returns_ok(self):
        """A successful PlexClient.get_libraries() call returns ok=True."""
        mock_client = type("FakePlex", (), {"get_libraries": lambda self: []})()
        with patch(
            "mediaman.services.media_meta.plex.PlexClient",
            return_value=mock_client,
        ):
            resp = _test_plex(self._settings())
        import json

        body = json.loads(resp.body)
        assert body["ok"] is True


@pytest.mark.parametrize(
    ("exc", "code", "message"),
    [
        (Unauthorized("401 secret-token"), "auth_failed", "Plex refused the token."),
        (NotFound("404"), "not_plex", "No Plex server answered at this address."),
        (BadRequest("400"), "not_plex", "No Plex server answered at this address."),
    ],
)
def test_plex_refusals_say_which_it_was_and_log_one_line(exc, code, message, caplog):
    import json
    import logging

    with (
        caplog.at_level(logging.INFO),
        patch("mediaman.services.media_meta.plex.PlexClient", side_effect=exc),
    ):
        resp = _test_plex({"plex_url": "http://u:p@plex:32400", "plex_token": "secret-token"})
    body = json.loads(resp.body)
    assert (body["ok"], body["error"], body["message"]) == (False, code, message)
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is None
    assert "secret-token" not in caplog.text and "u:p@" not in caplog.text
