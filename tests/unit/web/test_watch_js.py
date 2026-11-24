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


def test_the_season_tabs_are_one_line_that_shows_the_picked_one() -> None:
    assert _run("seasonLine") == {
        "row": True,
        "opened": [],
        "cutOff": [{"left": 300, "behavior": "smooth"}],
    }


def test_undo_after_moving_to_another_season_leaves_that_season_line_alone() -> None:
    second = _run("undoElsewhere")["second"]
    assert (second["text"], second["other"]) == ("Season 1 leaves in 3 days", True)


def test_the_kept_line_is_marked_kept_until_undo() -> None:
    out = _run("kept")
    assert (out["kept"]["text"], out["kept"]["kept"]) == ("Kept for 7 days", True)
    assert out["undone"]["kept"] is False
    assert out["undone"]["text"].startswith("Leaving in 3 days")


def test_a_kept_card_closes_its_slot_while_it_fades_without_squashing_its_content() -> None:
    out = _run("cardExit")
    # The whole exit fits --dur-slow (250ms); the slot starts closing at 0.6 of it.
    assert out["duration"] == 250
    assert out["lastOpen"] == 0.6
    assert out["lastOpen"] < out["faded"] <= 1
    assert all(width == "20px" for width in out["pinned"])


def test_a_swap_stacks_both_nodes_while_they_cross_fade_then_leaves_only_the_new_one() -> None:
    out = _run("swap")
    assert out["stacked"] is True
    assert out["cell"] == ["1 / 1", "1 / 1"]
    assert sorted(out["faded"]) == [["new", 0, 1], ["old", 1, 0]]
    assert out["after"] == ["new"]
    assert out["oldGone"] is True


def test_a_drop_or_a_newer_swap_during_a_fade_wins_over_the_older_fades_end() -> None:
    out = _run("swapInterrupted")
    assert out["dropped"] == []
    for ending in (out["olderEndsFirst"], out["newerEndsFirst"]):
        assert (ending["newerCell"], ending["oneStack"]) == ("1 / 1", True)
        assert "v2" in ending["between"]
        assert (ending["after"], ending["direct"], ending["cell"]) == (["v2"], True, "")


def test_a_swap_given_the_node_fading_out_still_ends_with_only_the_newest_view() -> None:
    out = _run("swapLeaving")
    for ending in (out["oldestEndsFirst"], out["newestEndsFirst"]):
        assert ending["during"]["shown"] == ["v3"]
        assert (ending["after"], ending["direct"], ending["inert"]) == (["v3"], True, False)


def test_a_swap_with_reduced_motion_is_immediate() -> None:
    out = _run("swapReduced")
    assert out == {"after": ["new"], "animated": False}


def test_flip_slides_only_the_nodes_that_moved_from_where_they_were() -> None:
    out = _run("flip")
    assert out["moved"] == [["a", "translate(-30px, -10px)", "none"]]


def test_what_has_eased_in_does_not_ease_in_again_when_a_swap_moves_it() -> None:
    assert _run("swapEased") == {"before": 2, "going": 0, "left": 0}


def test_focus_put_in_the_new_view_during_a_swap_outlives_the_fade() -> None:
    assert _run("swapFocus") == "go"


def test_the_toast_of_a_kept_season_names_it_once() -> None:
    assert _run("keptSeason") == ["Kept Glass Coast, Season 2 for 7 days."]


def test_a_finished_keep_and_a_finished_undo_are_announced() -> None:
    kept = ["mm:watch-kept", "1100"]
    assert _run("keepsTold") == {"early": 0, "kept": [kept], "undone": [kept, kept], "refused": 2}
