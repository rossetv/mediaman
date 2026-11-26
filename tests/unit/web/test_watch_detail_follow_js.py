"""The title overlay following what changes around it: a refused lookup, a window resized, an Arr slow
to answer, run under node against the fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_detail_follow_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_refused_lookup_says_there_are_too_many_not_that_something_broke() -> None:
    out = _run("tooMany")
    assert out["why"] == "Too many lookups for now. Try again in a while."
    assert out["retry"] is True


def test_keep_turns_into_a_link_and_back_as_the_window_changes_size() -> None:
    out = _run("keepFollowsSize")
    assert (out["wide"], out["narrow"], out["wideAgain"]) == (False, True, False)
    assert (
        out["words"]
        == out["wordsAgain"].replace(
            "Keep it to stop the deletion.", "Keep it to stop the deletion"
        )
        + "."
    )


def test_no_download_is_offered_until_the_arr_has_answered_and_it_is_asked_again_till_then() -> (
    None
):
    out = _run("arrSlow")
    assert out["first"] is None
    assert out["firstSays"] == "Checking if it is asked for"
    assert 2000 in out["waits"]
    # Still silent: nothing yet, and the next ask waits twice as long.
    assert out["second"] is None
    assert out["secondSays"] == "Checking if it is asked for"
    assert 4000 in out["waitsThen"] and 2000 not in out["waitsThen"]
    assert out["third"]["text"] == "Queued"
    assert out["thirdSays"] != "Checking if it is asked for"
    assert out["asks"] == 3


def test_an_arr_that_never_answers_is_asked_a_few_times_and_then_download_is_offered() -> None:
    out = _run("arrSilent")
    assert out["beforeLast"] is None
    assert out["beforeLastSays"] == "Checking if it is asked for"
    assert out["asks"] == 4
    assert "Download" in out["primary"]["text"] and not out["primary"]["disabled"]
    assert "answering" in out["says"] and "Checking" not in out["says"]
    # Pressed, it asks as ever and its own error says what failed.
    assert out["posts"] == ["/api/search/download"]
    assert "Radarr did not answer" in out["error"]
