"""Tests for the OpenAI recommendations service — model selection only.

The end-to-end recommendation flow hits external HTTP APIs (OpenAI, TMDB,
OMDb) so is not covered here; these tests lock in the model-selection
behaviour (no placeholder model slipping into production, settings-
overridable) without making any network calls.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mediaman.db import init_db
from mediaman.services import openai_recommendations


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


class TestOpenAIModelSelection:
    def test_default_model_is_not_placeholder(self):
        """The default model must not be the known-bad ``gpt-5.4`` placeholder."""
        default = openai_recommendations._DEFAULT_MODEL
        assert default
        assert default != "gpt-5.4", "Placeholder model must not ship as default"
        assert default.startswith("gpt-")

    def test_model_defaults_when_no_setting(self, conn):
        """When no ``openai_model`` setting is stored, the default is used."""
        assert openai_recommendations._get_openai_model(conn) == (
            openai_recommendations._DEFAULT_MODEL
        )

    def test_model_honours_setting(self, conn):
        """An ``openai_model`` setting overrides the default."""
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            ("openai_model", "gpt-4o", "2026-04-18T00:00:00+00:00"),
        )
        conn.commit()
        assert openai_recommendations._get_openai_model(conn) == "gpt-4o"

    def test_strip_season_suffix(self):
        """TV titles with a trailing ``Season N`` marker are cleaned for TMDB
        search; plain titles pass through unchanged. TMDB indexes the series
        title only, so without this a row like ``The Boys: Season 5`` would
        miss every lookup and land on /recommended with no poster or
        description."""
        strip = openai_recommendations._strip_season_suffix
        assert strip("The Boys: Season 5") == "The Boys"
        assert strip("Hacks: Season 5") == "Hacks"
        assert strip("Euphoria: Season 3") == "Euphoria"
        assert strip("The Boys Season 5") == "The Boys"
        assert strip("The Boys - Season 5") == "The Boys"
        assert strip("Stranger Things: S4") == "Stranger Things"
        assert strip("The Mummy") == "The Mummy"
        assert strip("Margo's Got Money Troubles") == "Margo's Got Money Troubles"

    def test_call_openai_sends_configured_model(self, conn, monkeypatch, fake_http, fake_response):
        """``_call_openai`` must forward the configured model in the request body."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "0123456789abcdef" * 4)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            ("openai_api_key", "sk-test", "2026-04-18T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
            ("openai_model", "gpt-4.1-mini", "2026-04-18T00:00:00+00:00"),
        )
        conn.commit()

        fake_http.queue("POST", fake_response(json_data={"output": []}))
        openai_recommendations._call_openai("hello", conn, use_web_search=False)

        post_call = next(c for c in fake_http.calls if c[0] == "POST")
        assert post_call[2]["json"]["model"] == "gpt-4.1-mini"
