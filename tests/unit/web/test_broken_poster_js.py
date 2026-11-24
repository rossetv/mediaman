"""Which pictures a hero treats as failed: those that did not load, and wide art that is not wide."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "broken_poster_harness.js"


def test_art_too_square_or_too_small_for_a_wide_hero_counts_as_failed() -> None:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr

    assert json.loads(result.stdout) == {
        "wide": False,
        "portrait": True,
        "square": True,
        "tiny": True,
        "poster": False,
        "failed": True,
        "earlyPortrait": True,
        "earlyWide": False,
        "pending": False,
    }
