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


def test_pressing_the_eye_keeps_the_cursor_where_it_was_in_the_field() -> None:
    out = _run("focus")
    assert out["prevented"] is True
    assert out["active"] is True
    assert out["kept"] == [2, 5, "backward"]
    # what the browser did stands once the field has been typed in or left
    assert out["typedMeanwhile"] == out["leftMeanwhile"] == [0, 0, "none"]
    # what shows the eye, which stays out of sight without the script
    assert out["drawn"] == "pwd-on"


def test_an_autofocus_field_keeps_its_focus_and_its_place_when_the_eye_is_wired() -> None:
    assert _run("autofocus") == {"active": True, "inPlace": True}


def test_the_eye_shows_and_hides_the_password_says_it_as_pressed_and_reset_hides_it() -> None:
    out = _run("toggle")
    hidden = {"type": "password", "pressed": "false", "name": "Show password", "title": "Show"}
    # the name stays and the state is announced as pressed, as on the password form
    assert out["before"] == out["hidden"] == out["reset"] == hidden
    assert out["shown"] == {
        "type": "text",
        "pressed": "true",
        "name": "Show password",
        "title": "Hide",
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
