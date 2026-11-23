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
    assert out["parts"][1] == "No matches for “zzqx”"
    assert out["parts"][2] != ""
    assert out["status"] == "No matches for “zzqx”"


def test_a_kept_card_closes_its_slot_while_it_fades_without_squashing_its_content() -> None:
    out = _run("cardExit")
    assert out["duration"] <= 350
    assert out["lastOpen"] < out["faded"]
    assert all(width == "20px" for width in out["pinned"])


def test_a_failed_search_says_so_in_one_alert_line_and_still_read_out() -> None:
    out = _run("searchError")
    assert (out["error"], out["alert"]) == (True, "alert")
    assert out["line"] == "Couldn’t search just now."
    assert out["status"] == "Couldn’t search just now."
    assert (out["retried"], out["focus"]) == (True, True)


def test_a_swap_stacks_both_nodes_while_they_cross_fade_then_leaves_only_the_new_one() -> None:
    out = _run("swap")
    assert out["stacked"] is True
    assert out["cell"] == ["1 / 1", "1 / 1"]
    assert sorted(out["faded"]) == [["new", 0, 1], ["old", 1, 0]]
    assert out["after"] == ["new"]
    assert out["oldGone"] is True


def test_a_swap_with_reduced_motion_is_immediate() -> None:
    out = _run("swapReduced")
    assert out == {"after": ["new"], "animated": False}


def test_flip_slides_only_the_nodes_that_moved_from_where_they_were() -> None:
    out = _run("flip")
    assert out["moved"] == [["a", "translate(-30px, -10px)", "none"]]
