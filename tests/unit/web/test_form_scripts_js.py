"""The password show/hide toggle and the submit guard, run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "form_scripts_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_wrapping_a_field_keeps_the_focus_it_had() -> None:
    out = _run("focus")
    assert out["wrapped"] == "pw-wrap"
    assert out["active"] is True


def test_an_autofocus_field_is_focused_after_it_is_wrapped() -> None:
    assert _run("autofocus")["active"] is True


def test_toggle_name_follows_the_visible_word_and_reset_hides_the_password_again() -> None:
    out = _run("toggle")
    assert out["name"] == "Show password"
    # the name starts with the visible word and the state is not also announced as pressed
    assert out["shown"] == {
        "type": "text",
        "text": "Hide",
        "pressed": None,
        "name": "Hide password",
    }
    assert out["reset"] == {
        "type": "password",
        "text": "Show",
        "pressed": None,
        "name": "Show password",
    }


def test_busy_form_swallows_a_quick_second_submit_then_lapses_or_clears_on_escape() -> None:
    out = _run("guard")
    assert (out["first"], out["quick"], out["busy"]) == (False, True, True)
    assert out["ms"][0] == 10000
    assert out["afterTimeout"] is False
    assert out["afterEnter"] is True
    assert out["afterEscape"] is False
    assert out["aria"] is None
