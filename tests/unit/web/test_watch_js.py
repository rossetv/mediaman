"""The show page's leaving line through season swaps and Keep, run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_season_that_is_not_leaving_keeps_the_leaving_line_slot_empty() -> None:
    out = _run("season")
    assert out["second"] == {"text": "", "kept": False, "beforeActions": True}
    assert out["back"]["text"].startswith("Leaving in 3 days")
    assert out["back"]["beforeActions"] is True


def test_undo_after_moving_to_another_season_leaves_that_season_slot_empty() -> None:
    assert _run("undoElsewhere")["second"]["text"] == ""


def test_the_kept_line_is_marked_kept_until_undo() -> None:
    out = _run("kept")
    assert (out["kept"]["text"], out["kept"]["kept"]) == ("Kept for 7 days", True)
    assert out["undone"]["kept"] is False
    assert out["undone"]["text"].startswith("Leaving in 3 days")
