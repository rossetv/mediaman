"""A person's page on Watch, run under node against a fake DOM with the search page as a stub:
what heads the titles the library does not hold."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_explore_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_titles_not_held_are_offered_for_download_only_where_both_kinds_can_be() -> None:
    assert _run("personHeads") == {
        "both": ["In your library", "Available to download"],
        "films": ["In your library", "Not in your library"],
        "none": ["In your library", "Not in your library"],
    }
