"""Tests for mediaman.services.openai.client."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import requests

from mediaman.db import init_db
from mediaman.services.openai import client as client_mod
from mediaman.services.openai.client import (
    _DEFAULT_MODEL,
    call_openai,
    get_openai_key,
    get_openai_model,
    is_web_search_enabled,
)
from tests.helpers.factories import insert_settings


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _put(conn, key, value):
    insert_settings(conn, **{key: value}, updated_at="2026-01-01")


class TestGetOpenAiKey:
    def test_raises_when_no_db_and_no_key(self, monkeypatch):
        """Environment variables are no longer accepted; ValueError is raised."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValueError, match="OpenAI API key is not configured"):
            get_openai_key(None)

    def test_returns_db_key_when_available(self, monkeypatch, conn):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")
        with patch(
            "mediaman.services.infra.settings_reader.get_string_setting", return_value="sk-db-key"
        ):
            result = get_openai_key(conn, secret_key="x" * 32)
        assert result == "sk-db-key"

    def test_raises_when_db_key_missing(self, monkeypatch, conn):
        """When DB has no key and env var is not consulted, ValueError is raised."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with (
            patch("mediaman.services.infra.settings_reader.get_string_setting", return_value=None),
            pytest.raises(ValueError, match="OpenAI API key is not configured"),
        ):
            get_openai_key(conn, secret_key="x" * 32)


class TestGetOpenAiModel:
    def test_default_model_returned_when_conn_is_none(self):
        assert get_openai_model(None) == _DEFAULT_MODEL

    def test_default_model_returned_when_no_setting(self, conn):
        assert get_openai_model(conn) == _DEFAULT_MODEL

    def test_custom_model_returned_when_setting_stored(self, conn):
        _put(conn, "openai_model", "gpt-6-luna")
        assert get_openai_model(conn) == "gpt-6-luna"

    def test_retired_model_falls_back_to_default(self, conn):
        _put(conn, "openai_model", "gpt-5.5")
        assert get_openai_model(conn) == _DEFAULT_MODEL


class TestIsWebSearchEnabled:
    def test_defaults_false_when_conn_is_none(self):
        assert is_web_search_enabled(None) is False

    def test_defaults_false_when_setting_absent(self, conn):
        assert is_web_search_enabled(conn) is False

    def test_returns_true_when_enabled(self, conn):
        _put(conn, "openai_web_search_enabled", "true")
        assert is_web_search_enabled(conn) is True


class TestCallOpenAi:
    def test_returns_empty_when_no_api_key(self, conn):
        with patch("mediaman.services.openai.client._OPENAI_CLIENT") as client:
            result = call_openai("some prompt", conn)
        assert result == []
        client.post.assert_not_called()

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

    def test_a_call_waits_two_minutes_and_is_sent_once(self, conn, monkeypatch):
        sent = []

        def time_out(path, **kwargs):
            sent.append((kwargs["timeout"], kwargs.get("retry", False)))
            raise requests.Timeout("slow")

        insert_settings(conn, openai_api_key="sk-test")
        monkeypatch.setattr(client_mod._OPENAI_CLIENT, "post", time_out)
        assert call_openai("p", conn) == []
        assert sent == [((5.0, 120.0), False)]

    @pytest.mark.parametrize(
        "payload", [{"output": None}, [], {"output": [None]}, "text", {"output": "[" * 5}]
    )
    def test_returns_empty_on_an_answer_of_another_shape(
        self, conn, fake_http, fake_response, payload
    ):
        insert_settings(conn, openai_api_key="sk-test")
        fake_http.queue("POST", fake_response(json_data=payload))
        assert call_openai("p", conn) == []

    def test_returns_empty_on_json_nested_too_deep(self, conn, fake_http, fake_response):
        insert_settings(conn, openai_api_key="sk-test")
        text = "[" * 200_000
        output = [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
        fake_http.queue("POST", fake_response(json_data={"output": output}))
        assert call_openai("p", conn) == []

    def test_how_much_a_model_may_write_is_bounded(self, conn, fake_http, fake_response):
        insert_settings(conn, openai_api_key="sk-test")
        fake_http.queue("POST", fake_response(json_data={"output": []}))
        call_openai("p", conn)
        assert 8_000 <= fake_http.calls[0][2]["json"]["max_output_tokens"] <= 32_000

    def test_returns_empty_on_http_error(self, conn, fake_http, fake_response):
        fake_http.queue("POST", fake_response(status=401, text="Unauthorised"))
        with patch("mediaman.services.openai.client.get_openai_key", return_value="sk-bad"):
            result = call_openai("some prompt", conn, use_web_search=False)
        assert result == []

    def test_web_search_tool_included_when_enabled(self, conn, fake_http, fake_response):
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

    @pytest.mark.parametrize(
        "title",
        [
            "Bad\x00Title",
            "",
            "A\u202etitle",
            "A\u200btitle",
            "A\u00a0title",
            "A\ue000",
            "[click me](http://evil.com)",
            "[Foo](evil)",
            "Dune https://evil.com",
            "Dune http://evil.com",
        ],
    )
    def test_web_search_batch_rejected_on_unsafe_title(self, conn, fake_http, fake_response, title):
        """If a title fails safety check, the entire batch is rejected."""
        payload = [
            {"title": "Dune", "media_type": "movie", "reason": "y"},
            {"title": title, "media_type": "movie", "reason": "x"},
        ]
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

    @pytest.mark.parametrize("title", ["Mr.Robot", "Dune at evil.xyz", "Dune (evil.co.uk)"])
    def test_a_title_shaped_like_a_domain_is_left_out_and_the_batch_kept(
        self, conn, fake_http, fake_response, title
    ):
        payload = [
            {"title": title, "media_type": "tv", "reason": "x"},
            {"title": "Dune", "media_type": "movie", "reason": "y"},
        ]
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
        assert [i["title"] for i in result] == ["Dune"]

    @pytest.mark.parametrize(
        "title",
        [
            "The Dark Knight (2008)",
            "S.W.A.T.",
            "Vol.2",
            "君の名は。",
            "Mr. Robot",
            "Monsters, Inc.",
            "Amélie",
            "アニメ",
            "Ocean’s – Eight",
        ],
    )
    def test_a_web_search_title_without_a_hostile_character_or_domain_is_kept(
        self, conn, fake_http, fake_response, title
    ):
        payload = [{"title": title, "media_type": "movie", "reason": "x"}]
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
        assert [i["title"] for i in result] == [title]

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
