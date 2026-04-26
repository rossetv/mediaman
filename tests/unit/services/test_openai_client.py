"""Tests for mediaman.services.openai.client."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from mediaman.db import init_db
from mediaman.services.openai.client import (
    _DEFAULT_MODEL,
    call_openai,
    get_openai_key,
    get_openai_model,
    is_web_search_enabled,
    validate_web_search_title,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _put(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, '2026-01-01')",
        (key, value),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# get_openai_key
# ---------------------------------------------------------------------------


class TestGetOpenAiKey:
    def test_returns_env_var_when_no_db(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key-test")
        assert get_openai_key(None) == "sk-env-key-test"

    def test_returns_none_when_no_env_and_no_db(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert get_openai_key(None) is None

    def test_env_not_checked_when_not_needed(self, monkeypatch, conn):
        """If DB has a key, env var value doesn't matter."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")
        with patch(
            "mediaman.services.infra.settings_reader.get_string_setting", return_value="sk-db-key"
        ):
            result = get_openai_key(conn, secret_key="x" * 32)
        assert result == "sk-db-key"


# ---------------------------------------------------------------------------
# get_openai_model
# ---------------------------------------------------------------------------


class TestGetOpenAiModel:
    def test_default_model_returned_when_conn_is_none(self):
        assert get_openai_model(None) == _DEFAULT_MODEL

    def test_default_model_returned_when_no_setting(self, conn):
        assert get_openai_model(conn) == _DEFAULT_MODEL

    def test_custom_model_returned_when_setting_stored(self, conn):
        _put(conn, "openai_model", "gpt-4o")
        assert get_openai_model(conn) == "gpt-4o"


# ---------------------------------------------------------------------------
# is_web_search_enabled
# ---------------------------------------------------------------------------


class TestIsWebSearchEnabled:
    def test_defaults_false_when_conn_is_none(self):
        assert is_web_search_enabled(None) is False

    def test_defaults_false_when_setting_absent(self, conn):
        assert is_web_search_enabled(conn) is False

    def test_returns_true_when_enabled(self, conn):
        _put(conn, "openai_web_search_enabled", "true")
        assert is_web_search_enabled(conn) is True


# ---------------------------------------------------------------------------
# validate_web_search_title
# ---------------------------------------------------------------------------


class TestValidateWebSearchTitle:
    def test_plain_ascii_title_is_valid(self):
        assert validate_web_search_title("Inception") is True

    def test_title_with_punctuation_is_valid(self):
        assert validate_web_search_title("The Dark Knight (2008)") is True

    def test_non_printable_ascii_rejected(self):
        assert validate_web_search_title("Title\x01here") is False

    def test_non_ascii_unicode_rejected(self):
        assert validate_web_search_title("Títle with accent") is False

    def test_markdown_link_rejected(self):
        assert validate_web_search_title("[click me](http://evil.com)") is False

    def test_embedded_https_url_rejected(self):
        assert validate_web_search_title("Dune https://evil.com") is False

    def test_embedded_http_url_rejected(self):
        assert validate_web_search_title("Dune http://evil.com") is False


# ---------------------------------------------------------------------------
# call_openai
# ---------------------------------------------------------------------------


class TestCallOpenAi:
    def test_returns_empty_when_no_api_key(self, conn):
        with patch("mediaman.services.openai.client.get_openai_key", return_value=None):
            result = call_openai("some prompt", conn)
        assert result == []

    def test_parses_valid_json_response(self, conn, fake_http, fake_response):
        payload = [{"title": "Dune", "media_type": "movie", "reason": "Great film"}]
        openai_resp = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(payload)}],
                }
            ]
        }
        fake_http.queue("POST", fake_response(json_data=openai_resp))
        with patch("mediaman.services.openai.client.get_openai_key", return_value="sk-test"):
            result = call_openai("some prompt", conn, use_web_search=False)
        assert result == payload

    def test_returns_empty_on_non_list_json(self, conn, fake_http, fake_response):
        openai_resp = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"title": "Dune"}'}],
                }
            ]
        }
        fake_http.queue("POST", fake_response(json_data=openai_resp))
        with patch("mediaman.services.openai.client.get_openai_key", return_value="sk-test"):
            result = call_openai("some prompt", conn, use_web_search=False)
        assert result == []

    def test_returns_empty_on_http_error(self, conn, fake_http, fake_response):
        fake_http.queue("POST", fake_response(status=401, text="Unauthorised"))
        with patch("mediaman.services.openai.client.get_openai_key", return_value="sk-bad"):
            result = call_openai("some prompt", conn, use_web_search=False)
        assert result == []

    def test_web_search_tool_included_when_enabled(self, conn, fake_http, fake_response):
        """When web search is enabled, the request body includes the tool."""
        payload = [{"title": "Severance", "media_type": "tv", "reason": "Great show"}]
        openai_resp = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(payload)}],
                }
            ]
        }
        fake_http.queue("POST", fake_response(json_data=openai_resp))
        with (
            patch("mediaman.services.openai.client.get_openai_key", return_value="sk-test"),
            patch("mediaman.services.openai.client.is_web_search_enabled", return_value=True),
        ):
            result = call_openai("some prompt", conn, use_web_search=True)
        _, _, kwargs = fake_http.calls[0]
        assert "tools" in kwargs["json"]
        assert result == payload

    def test_web_search_batch_rejected_on_unsafe_title(self, conn, fake_http, fake_response):
        """If a title fails safety check, the entire batch is rejected."""
        payload = [{"title": "Bad\x00Title", "media_type": "movie", "reason": "x"}]
        openai_resp = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(payload)}],
                }
            ]
        }
        fake_http.queue("POST", fake_response(json_data=openai_resp))
        with (
            patch("mediaman.services.openai.client.get_openai_key", return_value="sk-test"),
            patch("mediaman.services.openai.client.is_web_search_enabled", return_value=True),
        ):
            result = call_openai("some prompt", conn, use_web_search=True)
        assert result == []

    def test_strips_markdown_code_fence_from_response(self, conn, fake_http, fake_response):
        """Model sometimes wraps JSON in ```json fences despite instructions."""
        payload = [{"title": "Dune", "media_type": "movie", "reason": "Epic"}]
        raw = "```json\n" + json.dumps(payload) + "\n```"
        openai_resp = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": raw}],
                }
            ]
        }
        fake_http.queue("POST", fake_response(json_data=openai_resp))
        with patch("mediaman.services.openai.client.get_openai_key", return_value="sk-test"):
            result = call_openai("some prompt", conn, use_web_search=False)
        assert result == payload
