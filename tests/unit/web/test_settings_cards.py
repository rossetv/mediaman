"""Settings' integration cards and the buttons around them: what they ask for.

The save runs under node against the fake DOM; the markup is the real template rendered.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jinja2

WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
JS = Path(__file__).parent / "js"


def _node(harness: str, scenario: str, *args: str):
    result = subprocess.run(
        ["node", str(JS / harness), scenario, *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def _integrations() -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB / "templates"), autoescape=True)
    return env.get_template("settings/_sec_integrations.html").render(settings={})


# The TMDB card


def test_the_tmdb_card_asks_only_for_the_read_token_the_app_uses() -> None:
    html = _integrations()
    assert 'id="tmdb_api_key"' not in html
    assert 'id="tmdb_read_token"' in html


def test_a_save_no_longer_sends_the_tmdb_api_key() -> None:
    keys = _node("settings_harness.js", "payloadKeys")
    assert "tmdb_read_token" in keys
    assert "tmdb_api_key" not in keys
