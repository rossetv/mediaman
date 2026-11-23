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


def test_the_message_fades_and_takes_its_own_line_on_a_phone() -> None:
    css = (_CSS / "_settings_pg.css").read_text()
    base = css.split(".reauth-sheet .reauth-msg {")[1].split("}")[0]
    assert "opacity: 0" in base
    assert "transition: opacity var(--dur-fast) var(--ease-out)" in base
    assert re.search(r"\.reauth-msg\.is-shown \{ opacity: 1; \}", css)
    phone = css.split(".reauth-msg.is-shown")[1].split("\n}")[0]
    assert ".reauth-actions { flex-wrap: wrap; }" in phone
    assert "flex: 0 0 100%" in phone


def test_dialog_buttons_are_44px_tall_on_a_phone() -> None:
    css = (_CSS / "_modal.css").read_text()
    phone = css.split("@media (max-width: 639.98px) {")[1].split("\n}")[0]
    assert re.search(
        r"\.modal-sheet \.btn,\s*\.ui-confirm-actions \.btn \{ min-height: 44px; \}", phone
    )
