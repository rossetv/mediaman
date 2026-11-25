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


def test_a_picture_of_our_own_image_route_is_tried_twice_more_before_it_is_marked() -> None:
    out = _run()["again429"]
    assert out["thenLoads"] == [[False, 0], [False, 1], [False, 1]]
    assert out["early"] == [[False, 1], [False, 1]]
    assert out["neverLoads"] == [
        [False, 0],
        [False, 1],
        [False, 1],
        [False, 2],
        [True, 2],
        [True, 2],
    ]
    # One that loaded and failed later has its tries back.
    assert out["afresh"][-2:] == [[False, 3], [True, 3]]


def test_only_a_picture_still_wanted_at_that_address_is_tried_again() -> None:
    out = _run()["again429"]
    assert out["otherWhileWaiting"][-1] == [False, 0]
    assert out["gone"][-1] == [False, 0]
    assert out["hidden"][-1] == [True, 0]
    assert out["elsewhere"] == [[True, 0], [True, 0]]
    assert out["next"] == [[False, 1], [True, 1]]
    assert out["unfit"] == [[True, 0], [True, 0]]
