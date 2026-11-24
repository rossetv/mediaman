"""Which pictures a hero treats as failed: those that did not load, and wide art that is not wide."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "broken_poster_harness.js"


def _run() -> dict:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_art_too_square_or_too_small_for_a_wide_hero_counts_as_failed() -> None:
    assert _run()["marks"] == {
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


def test_a_picture_with_another_to_try_is_marked_only_when_that_one_fails_too() -> None:
    out = _run()["retries"]
    trying = {"src": "/next.jpg", "marked": False, "retrying": True}
    shown = {"src": "/next.jpg", "marked": False, "retrying": False}
    given_up = {"src": "/next.jpg", "marked": True, "retrying": False}

    for name in ("unfitThenWide", "goneThenWide", "early", "earlyGone"):
        assert out[name] == [trying, shown], name
    for name in ("unfitTwice", "unfitThenGone"):
        assert out[name] == [trying, given_up], name
    assert out["wide"][0] == {"src": None, "marked": False, "retrying": False}
