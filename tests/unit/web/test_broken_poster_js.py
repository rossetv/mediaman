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


def test_only_a_picture_that_fits_is_marked_as_one_and_at_once_when_it_had_loaded_before() -> None:
    assert _run()["fits"] == {
        "loads": "",
        "thenTall": None,
        "thenWide": "",
        "thenGone": None,
        "early": "at-once",
        "earlyThenItsEvent": "at-once",
        "earlyTall": None,
        "pending": None,
        "poster": None,
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


def test_a_picture_that_failed_is_marked_at_once_and_tried_twice_behind_its_stand_in() -> None:
    out = _run()["tries"]
    assert out["comesBack"]["seen"] == [[True, 0, 0], [True, 1, 0], [False, 1, 1], [False, 1, 1]]
    assert out["second"]["seen"][3:] == [[True, 2, 0], [False, 2, 1], [False, 2, 1]]
    assert out["early"]["seen"] == [[True, 1, 0], [False, 1, 1]]
    assert out["never"]["seen"][-1] == [True, 2, 0]
    assert all(marked for marked, *_ in out["never"]["seen"])
    # What the try found may fail on the page all the same; that does not go on for ever.
    assert out["failsAgain"]["seen"][-1] == [True, 2, 2]
    # One that loaded and failed later has its tries back.
    assert out["laterAgain"]["seen"][-1] == [True, 3, 1]


def test_a_picture_that_comes_back_fades_in_unless_motion_is_reduced() -> None:
    out = _run()["tries"]
    assert out["comesBack"]["fades"] == out["second"]["fades"] == 1
    assert out["calm"]["fades"] == 0
    assert out["plain"]["fades"] == 0


def test_only_a_picture_still_wanted_at_that_address_is_tried_again() -> None:
    out = {name: got["seen"][-1] for name, got in _run()["tries"].items()}
    assert out["otherWaiting"] == [False, 0, 0]
    assert out["otherAsking"] == out["afreshAsking"] == [False, 1, 0]
    assert out["gone"] == out["hidden"] == [True, 0, 0]
    assert out["goneAsking"] == out["hiddenAsking"] == [True, 1, 0]
    assert out["elsewhere"] == out["unfit"] == [True, 0, 0]
    assert out["next"] == [True, 0, 1]


def test_pictures_that_failed_together_are_not_tried_together() -> None:
    waits = _run()["waits"]
    assert len(set(waits)) == len(waits)
