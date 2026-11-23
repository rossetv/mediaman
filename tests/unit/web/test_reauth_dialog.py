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
