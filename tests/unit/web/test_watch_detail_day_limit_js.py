"""The title overlay when the day's lookups are used up, run under node against the fake DOM."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_detail_day_limit_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "TZ": "UTC", "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_used_up_day_says_when_the_lookups_come_back() -> None:
    out = _run("soon")
    assert out["line"] == "Today's lookups are used up"
    assert re.fullmatch(r"Back at (2:30\s?PM|14:30)\.", out["why"]), out["why"]
    assert out["retry"] is True


def test_a_wait_of_more_than_half_a_day_names_the_day() -> None:
    out = _run("tomorrow")
    assert re.fullmatch(r"Back Wednesday,? (at )?(6:00\s?PM|18:00)\.", out["why"]), out["why"]


def test_a_used_up_day_without_a_time_does_not_invent_one() -> None:
    assert _run("noWait")["why"] == "Try again tomorrow."


def test_any_other_refusal_keeps_its_words() -> None:
    out = _run("other")
    assert out["line"] == "Couldn't load details"
    assert out["why"] == "Too many lookups for now. Try again in a while."
