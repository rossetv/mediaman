"""ui-feedback.js run under node against a fake DOM: when Ctrl+Z presses a toast's Undo."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "ui_feedback_harness.js"


def test_undo_answers_ctrl_z_unless_a_dialog_on_screen_or_a_text_field_has_the_keyboard() -> None:
    result = subprocess.run(
        ["node", str(HARNESS)], capture_output=True, text=True, check=False, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
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
