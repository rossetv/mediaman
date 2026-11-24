"""The re-authenticate dialog: its message fades in and out, and its phone layout."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import mediaman.web

HARNESS = Path(__file__).parent / "js" / "reauth_harness.js"
_CSS = Path(mediaman.web.__file__).parent / "static" / "css"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_refusal_and_an_empty_password_fade_the_message_in() -> None:
    out = _run("error")
    assert out["before"] == {"text": "", "on": False}
    assert out["failed"] == {"text": "Password is incorrect", "on": True}
    assert out["required"] == {"text": "Password required.", "on": True}


def test_the_message_fades_out_before_it_is_emptied() -> None:
    out = _run("retry")
    assert out["fading"] == {"text": "Password is incorrect", "on": False}
    assert out["emptied"] == {"text": "", "on": False}


def test_the_message_fades_and_the_buttons_stack_like_the_confirm_dialog_on_a_phone() -> None:
    css = (_CSS / "_settings_pg.css").read_text()
    base = css.split(".reauth-sheet .reauth-msg {")[1].split("}")[0]
    assert "opacity: 0" in base
    assert "transition: opacity var(--dur-fast) var(--ease-out)" in base
    assert re.search(r"\.reauth-msg\.is-shown \{ opacity: 1; \}", css)
    phone = (
        css.split(".reauth-msg.is-shown { opacity: 1; }")[1]
        .split("@media (max-width: 699.98px) {")[1]
        .split("\n}")[0]
    )
    assert "flex-direction: column" in phone
    assert re.search(r"\.reauth-actions \.btn \{ width: 100%; min-height: 44px; \}", phone)
    assert "order: -1" in phone  # the message stays on its own line above the buttons
    # Cancel comes before the main button in the markup, as in the confirm dialog
    js = (_CSS.parent / "js" / "core" / "reauth.js").read_text()
    assert js.index("cancelBtn);") < js.index("confirmBtn);")


def test_dialog_buttons_are_44px_tall_on_a_phone() -> None:
    # every .btn, the dialog's own included, is 44px tall below 700px
    css = (_CSS / "_buttons.css").read_text()
    phone = css.split("@media (max-width: 699.98px) {")[1].split("\n}")[0]
    assert re.search(r"(^|,)\s*\.btn,[^{]*\{ min-height: 44px; \}", phone)


def test_cancelling_the_dialog_returns_focus_to_the_control_that_asked() -> None:
    assert _run("focusReturn") == {"restored": True}


def test_pressing_enter_again_sends_the_password_and_the_protected_request_no_second_time() -> None:
    # the protected request runs twice: refused for want of the password, then with it
    assert _run("again") == {"passwords": 1, "runs": 2}


def test_enter_held_down_presses_nothing_after_the_dialog_until_it_is_let_go() -> None:
    # the focus goes back to the button that asked, which the repeats would press again
    assert _run("heldKey") == {
        "enter": True,
        "space": True,
        "aNewPress": False,
        "otherKey": False,
        "afterRelease": False,
    }


def test_enter_held_down_after_a_refusal_sends_the_password_no_second_time() -> None:
    assert _run("heldAfterRefusal") == {"held": 1, "pressedAgain": 2}


def test_an_answer_that_comes_after_the_dialog_was_closed_does_nothing() -> None:
    quiet = {"keyboardStays": True, "cancels": 1, "said": ""}
    assert _run("lateAnswer") == {"refused": quiet, "accepted": quiet}


def test_a_request_can_word_its_own_dialog_and_a_save_keeps_the_usual_words() -> None:
    out = _run("words")
    assert out["own"] == ["T", "D", "C"]
    assert (out["none"][0], out["none"][2]) == ("Re-authenticate to save", "Confirm & save")


def test_the_dialog_is_named_by_its_title_and_described_by_its_reason() -> None:
    out = _run("named")
    assert out["name"] == "Re-authenticate to save"
    assert "password" in out["about"]


def test_the_dialog_holds_the_width_of_the_page_s_scrollbar_until_it_closes() -> None:
    assert _run("bar") == {"held": "15px", "after": None}


def test_a_closed_dialog_leaves_the_keyboard_on_no_field_of_its_own() -> None:
    # Safari scrolls the page to its end when a field is removed with the keyboard in it
    assert _run("letGo") == {"had": True, "kept": False}
