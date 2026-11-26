"""Settings' integration cards and the buttons around them: names, words and folds.

The scripts run under node against the fake DOM (js/settings_cards_harness.js); the
markup is the real template rendered.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jinja2
import pytest

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


def _cards(scenario: str, *args: str):
    return _node("settings_cards_harness.js", scenario, *args)


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


# Sync now


def test_sync_now_says_how_many_items_synced() -> None:
    out = _cards("sync", json.dumps({"ok": True, "synced": 1204, "errors": 0, "removed": 0}))
    assert out["shown"] == "Synced ✓"
    assert [kind for kind, _ in out["said"]] == ["success"]
    assert "1,204 items" in out["said"][0][1]
    assert out["after"] == "Sync now"


def test_sync_now_with_nothing_synced_says_so_and_where_to_choose_libraries() -> None:
    out = _cards("sync", json.dumps({"ok": True, "synced": 0, "errors": 0, "removed": 0}))
    assert out["shown"] == "0 synced"
    (kind, words), *_ = out["said"]
    assert kind != "success"
    assert "Nothing synced" in words and "Libraries & Paths" in words


@pytest.mark.parametrize(("synced", "start"), [(10, "Synced 10 items"), (0, "Nothing synced")])
def test_sync_now_counts_what_failed(synced: int, start: str) -> None:
    out = _cards("sync", json.dumps({"ok": True, "synced": synced, "errors": 3, "removed": 0}))
    assert out["shown"] == "3 failed"
    assert out["said"][0][0] == "error"
    assert out["said"][0][1].startswith(start) and "3 failed" in out["said"][0][1]
