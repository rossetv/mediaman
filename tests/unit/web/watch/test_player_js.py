"""player.js in a fake DOM: quality switches.

Each test renders the real player markup, runs player.js on it through
``player_harness.js`` under node, and checks what the person would see.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jinja2
import pytest

REPO = Path(__file__).resolve().parents[4]
HARNESS = Path(__file__).with_name("player_harness.js")
# The page's own markup, without the site chrome around it.
_ENV = jinja2.Environment(
    loader=jinja2.ChoiceLoader(
        [
            jinja2.DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            jinja2.FileSystemLoader(REPO / "src/mediaman/web/templates"),
        ]
    ),
    autoescape=True,
)

EPISODE = {
    "item": {
        "rating_key": "501",
        "title": "The Long Coast",
        "subtitle": "S2 · E4",
        "duration_s": 3000.0,
    },
    "start_s": 0,
    "back_href": "/watch/show/77",
    "next": {"href": "/watch/play/502", "label": "S2 · E5 — High Tide"},
    "show_href": "/watch/show/77",
    "leaving": None,
}


@pytest.fixture(scope="module")
def markup(tmp_path_factory) -> Path:
    html = _ENV.get_template("watch_player.html").render(**EPISODE)
    path = tmp_path_factory.mktemp("player") / "player.html"
    path.write_text(html)
    return path


def run(markup: Path, scenario: str, *, reduced: bool = False) -> dict:
    args = ["node", str(HARNESS), str(markup), scenario] + (["reduced"] if reduced else [])
    done = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_quality_switch_holds_the_last_frame_until_the_new_stream_plays(markup):
    out = run(markup, "quality_switch")
    assert out["whileLoading"]["hold"] == {"shown": True, "drewVideo": True}
    assert out["afterManifest"]["hold"]["shown"] is True
    assert out["afterPlaying"]["hold"]["shown"] is False


def test_a_quality_switch_shows_loading_rather_than_pause(markup):
    out = run(markup, "quality_switch")
    loading = out["whileLoading"]
    assert loading["loading"] is True
    assert loading["play"]["busy"] == "true"
    assert "fa-pause" not in loading["play"]["icon"]
    assert out["afterPlaying"]["play"] == {
        "label": "Pause",
        "busy": None,
        "icon": "fa-solid fa-pause",
    }


def test_a_refused_quality_goes_back_to_the_one_that_played_and_keeps_the_frame(markup):
    out = run(markup, "quality_refused")
    assert out["beforePlaying"]["qualities"] == ["best", "low", "low", "best"]
    assert out["beforePlaying"]["hold"]["shown"] is True
    assert out["afterPlaying"]["hold"]["shown"] is False
    assert out["checked"] == "best"
    assert out["saved"] == "best"
    assert [t["kind"] for t in out["toasts"]] == ["error"]
    assert "Best" in out["toasts"][0]["text"]
