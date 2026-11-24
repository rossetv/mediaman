"""player.js in a fake DOM: quality switches, the next-episode card, keys and controls.

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


def run(
    markup: Path, scenario: str, *, reduced: bool = False, saved: dict[str, str] | None = None
) -> dict:
    args = ["node", str(HARNESS), str(markup), scenario] + (["reduced"] if reduced else [])
    args += [json.dumps(saved)] if saved else []
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
    assert loading["play"]["icon"] is None
    assert loading["play"]["ring"] is True
    assert out["afterPlaying"]["play"] == {
        "label": "Pause",
        "busy": None,
        "icon": "#i-pause",
        "ring": False,
    }


def test_the_centre_spinner_is_hidden_while_a_frame_is_held_and_back_after(markup):
    # The stylesheet hides the spinner under .is-held; the play button's ring stays.
    out = run(markup, "quality_switch")
    assert out["whileLoading"]["loading"] is True
    assert out["whileLoading"]["held"] is True
    assert out["afterManifest"]["held"] is True
    assert out["afterPlaying"]["held"] is False


def test_a_plain_stall_still_shows_the_centre_spinner(markup):
    out = run(markup, "stall")
    assert out == {"loading": True, "held": False}


def test_a_refused_quality_goes_back_to_the_one_that_played_and_keeps_the_frame(markup):
    out = run(markup, "quality_refused")
    assert out["beforePlaying"]["qualities"] == ["best", "low", "low", "best"]
    assert out["beforePlaying"]["hold"]["shown"] is True
    assert out["afterPlaying"]["hold"]["shown"] is False
    assert out["checked"] == "best"
    assert out["saved"] == "best"
    assert [t["kind"] for t in out["toasts"]] == ["error"]
    assert "Best" in out["toasts"][0]["text"]


def test_cancel_waits_for_the_card_to_leave_then_returns_focus(markup):
    out = run(markup, "upnext_cancel")
    assert out["shown"]["hidden"] is False
    assert out["afterClick"] == {"hidden": False, "leaving": True}
    assert out["afterExit"] == {"hidden": True, "leaving": False, "focus": True}
    assert out["href"] == "/watch/play/1"


def test_the_countdown_fill_lasts_the_countdown(markup):
    assert run(markup, "upnext_cancel")["shown"]["fill"] == "10s"


def test_cancel_under_reduced_motion_hides_the_card_at_once(markup):
    out = run(markup, "upnext_cancel", reduced=True)
    assert out["afterClick"]["hidden"] is True


@pytest.mark.parametrize("way", ["click", "touch", "space", "escape", "button"])
def test_a_stop_during_the_countdown_stays_at_the_end_and_never_replays(markup, way):
    out = run(markup, "upnext_stops")
    assert out[way] == {"counting": "Pause", "replayed": 0, "leaving": True, "after": "Play"}
    assert out["href"] == "/watch/play/1"


def test_after_the_countdown_only_play_itself_replays(markup):
    out = run(markup, "upnext_stops")
    assert (out["pictureReplays"], out["buttonReplays"]) == (0, 1)
