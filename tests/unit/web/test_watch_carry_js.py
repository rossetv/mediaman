"""How a preview becomes its title's page under a scroll, run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_carry_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_staging_that_fails_part_way_leaves_the_page_as_it_was() -> None:
    assert _run("throwsAfterStaging") == {
        "threw": True,
        "carried": False,
        "cardCarried": False,
        "ghost": 0,
        "plate": 0,
        "shade": 0,
        "title": "Fieldwork One",
        "words": 0,
    }
