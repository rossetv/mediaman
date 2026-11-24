"""ui-feedback.js run under node against a fake DOM: when Ctrl+Z presses a toast's Undo,
and confirms asked together."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "ui_feedback_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False, timeout=20
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_dismissing_answers_every_confirm_still_open_and_no_other() -> None:
    assert _run("stacked") == [
        {"ids": 2, "answers": [False, True]},
        {"ids": 2, "answers": [True, False]},
        {"ids": 2, "answers": [False, False]},
    ]


def test_undo_answers_ctrl_z_unless_a_dialog_on_screen_or_a_text_field_has_the_keyboard() -> None:
    assert _run("pressed") == {
        "nothingOpen": True,
        # a dialog in something that is not shown
        "closedInside": True,
        "open": False,
        # a toast from the dialog that is open
        "itsOwn": True,
        "anotherOverItsOwn": False,
        "confirmOverItsOwn": False,
        "textFieldInItsOwn": False,
        "ariaHidden": True,
        "invisible": True,
        "notModal": True,
        "confirm": False,
        "textField": False,
        "redo": False,
    }
