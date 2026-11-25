"""The Refresh of the shared picks in Settings, run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from mediaman.app_factory import _TEMPLATE_DIR

HARNESS = Path(__file__).parent / "js" / "settings_picks_harness.js"
NAME = "Refresh shared picks for everyone"
READY = {"text": NAME, "off": False, "label": NAME}


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_refresh_is_in_the_ai_service_s_card_alone() -> None:
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)
    html = env.get_template("settings/_sec_integrations.html").render(settings={})
    assert html.count('id="btn-refresh-picks"') == 1
    openai = html.split('id="intg-openai"')[1].split('id="intg-tmdb"')[0]
    assert f'id="btn-refresh-picks">{NAME}</button>' in openai


def test_the_wait_is_on_the_button_until_it_is_over() -> None:
    out = _run("waits")
    assert out["look"] == {
        "text": "Refresh shared picks in 14 h",
        "off": True,
        "label": f"{NAME}, available in 14 h",
    }
    assert out["said"] == [["toast", "The shared picks can be refreshed again in 14 h."]]
    assert (out["posts"], out["over"]) == (0, READY)


def test_a_refresh_is_followed_to_its_end_and_starts_the_wait() -> None:
    out = _run("done")
    assert out["ready"] == READY
    assert (out["busy"]["text"], out["busy"]["off"]) == ("Refreshing shared picks…", True)
    assert out["calls"].count("POST /api/recommended/refresh") == 1
    assert out["said"] == [["success", "New shared picks are on Watch."]]
    assert out["after"]["text"] == "Refresh shared picks in 24 h"


def test_a_refresh_that_made_none_died_or_is_out_of_reach_can_be_tried_again() -> None:
    out = _run("failed")
    assert out["none"] == {"look": READY, "said": [["error", "No key quota"]]}
    assert out["died"]["once"] == "Refreshing shared picks…"
    assert (out["died"]["look"], len(out["died"]["said"])) == (READY, 1)
    assert (out["offline"]["look"], len(out["offline"]["said"])) == (READY, 1)
    assert out["offline"]["timers"] == 0


def test_a_refusal_for_the_wait_shows_it_and_any_other_says_so() -> None:
    out = _run("refused")
    assert out["wait"]["look"]["text"] == "Refresh shared picks in 20 h"
    assert out["wait"]["said"] == []
    assert out["broken"]["look"] == READY
    assert out["broken"]["said"][0][0] == "error"


def test_it_is_off_without_a_stored_key_or_with_recommendations_off_and_says_why() -> None:
    out = _run("needs")
    assert out["key"] == {
        "off": True,
        "said": [["toast", "Save an OpenAI key first."]],
        "posts": 0,
    }
    assert (out["on"]["off"], out["on"]["posts"]) == (True, 0)
    assert out["on"]["said"] == [["toast", "AI recommendations are switched off."]]
    assert out["saved"] is False


def test_a_session_that_ends_goes_to_sign_in_at_once_but_not_over_unsaved_edits() -> None:
    out = _run("loggedOut")
    login = "/login?next=%2Fsettings"
    for clean in (out["clean"], out["demoted"], out["pressClean"]):
        assert (clean["href"], clean["said"]) == (login, [])
    assert (out["clean"]["polls"], out["demoted"]["polls"]) == (1, 1)
    for kept in (out["edits"], out["demotedEdits"], out["press"]):
        assert (kept["href"], len(kept["said"]), kept["text"]) == ("/settings", 1, NAME)
        assert kept["polls"] <= 1
    assert out["edits"]["said"] == out["press"]["said"]
    assert "session has expired" in out["edits"]["said"][0]


def test_one_already_running_is_followed_and_one_long_over_is_not_told() -> None:
    out = _run("running")
    assert out["busy"]["off"] is True
    assert len(out["said"]) == 1
    assert out["old"] == []
