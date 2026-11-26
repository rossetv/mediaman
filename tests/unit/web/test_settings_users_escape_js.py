"""Escape shuts the e-mail form of a Team row, as it shuts Add user."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "settings_harness.js"


def test_escape_shuts_a_rows_email_form_and_gives_the_focus_back() -> None:
    result = subprocess.run(
        ["node", str(HARNESS), "emailEscape"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"opened": "true", "expanded": "false", "focused": True}
