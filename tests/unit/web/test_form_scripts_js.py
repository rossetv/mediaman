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


def test_busy_form_swallows_a_quick_second_submit_then_lapses_and_ignores_escape() -> None:
    out = _run("guard")
    assert (out["first"], out["quick"], out["busy"]) == (False, True, True)
    assert out["ms"][0] == 10000
    assert out["afterTimeout"] is False
    assert out["afterEnter"] is True
    assert out["afterEscape"] is True
    assert out["afterFreshShow"] is True
    assert out["afterRestore"] is False
    assert out["aria"] is None


def test_a_busy_button_shows_its_busy_label_and_gets_its_own_back() -> None:
    out = _run("busyLabel")
    assert out["whileBusy"] == "Unsubscribing…"
    assert out["second"] is True
    assert out["stillBusy"] == "Unsubscribing…"
    assert out["afterLapse"] == "Unsubscribe"
    assert out["busyAfter"] is False


def test_only_the_pressed_button_is_busy_and_it_gets_its_own_words_back() -> None:
    out = _run("pressed")
    idle = [["7 days", "", "", ""], ["30 days", "", "", ""], ["90 days", "", "", ""]]
    assert out["busy"] == [idle[0], ["Keeping…", "is-pressed", "btn-spinner", "120px"], idle[2]]
    assert out["after"] == idle


def test_a_busy_button_is_drawn_before_its_post_which_goes_once_and_names_it() -> None:
    out = _run("held")
    assert out["before"] == []
    assert out["drawn"] == out["after"] == ["b"]
    assert out["lapsed"] == 2


def test_a_held_post_is_never_lost() -> None:
    out = _run("held")
    assert out["undrawn"] == ["b"], "a tab that draws nothing"
    assert out["noSubmitter"] == [None]
    assert out["invalid"] == {"posts": [], "busy": False}
    assert out["thenValid"] == ["b"]
    assert out["old"] == ["b"], "a browser without requestSubmit"


def test_a_form_whose_button_shows_no_progress_posts_at_once() -> None:
    assert _run("held")["unlabelled"] == ["b"]
