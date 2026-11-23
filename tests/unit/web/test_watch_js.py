"""The Watch pages' leaving line, Keep and search, run under node against a fake DOM."""

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


def test_a_season_switch_shows_the_quiet_line_then_the_real_one_in_the_same_slot() -> None:
    out = _run("season")
    assert out["second"] == {
        "text": "Season 1 leaves in 3 days",
        "kept": False,
        "other": True,
        "keep": False,
        "beforeActions": True,
    }
    assert out["back"]["text"].startswith("Leaving in 3 days")
    assert (out["back"]["other"], out["back"]["keep"]) == (False, True)
    assert out["back"]["beforeActions"] is True


def test_undo_after_moving_to_another_season_leaves_that_season_line_alone() -> None:
    second = _run("undoElsewhere")["second"]
    assert (second["text"], second["other"]) == ("Season 1 leaves in 3 days", True)


def test_the_kept_line_is_marked_kept_until_undo() -> None:
    out = _run("kept")
    assert (out["kept"]["text"], out["kept"]["kept"]) == ("Kept for 7 days", True)
    assert out["undone"]["kept"] is False
    assert out["undone"]["text"].startswith("Leaving in 3 days")


def test_an_empty_search_shows_a_hint_and_reads_out_only_the_line() -> None:
    out = _run("searchEmpty")
    assert out["empty"] is True
    assert out["parts"][1] == "Nothing found for “zzqx”."
    assert out["parts"][2] != ""
    assert out["status"] == "Nothing found for “zzqx”."


def test_a_kept_card_closes_its_slot_while_it_fades_without_squashing_its_content() -> None:
    out = _run("cardExit")
    assert out["duration"] <= 350
    assert out["lastOpen"] < out["faded"]
    assert all(width == "20px" for width in out["pinned"])
