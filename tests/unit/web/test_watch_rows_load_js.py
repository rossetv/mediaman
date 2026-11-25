"""The Watch page's row loader on cards that stand in lines, run under node against a fake DOM.
Its sideways rows are in test_watch_browse_js.py."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_rows_load_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_lines_load_to_the_end_with_no_title_twice() -> None:
    out = _run("toTheEnd")
    assert out["refs"] == [1, 2, 3, 4]
    assert out["asked"] == ["p0", "p1", "p2"]
    assert out["took"] == [[1, False], [0, False], [1, True]]
    assert (out["during"], out["left"], out["next"], out["busy"]) == (5, 0, None, None)


def test_each_season_of_a_show_is_a_card_of_its_own_and_comes_once() -> None:
    assert _run("seasons") == ["season/71", "season/72", "show/8", "season/73"]


def test_a_title_with_no_page_comes_once_by_its_key() -> None:
    assert _run("noPage") == ["id:5", "id:4", None, "id:3", None]


def test_too_many_requests_waits_and_goes_on_without_an_error() -> None:
    out = _run("waitsAfterTooMany")
    assert out["waiting"] == {"placeholders": 5, "failed": 0, "ms": [5000]}
    assert out["refs"] == [1, 2]
    assert out["asked"] == ["p0", "p0"]


def test_a_failure_holds_the_place_until_try_again_brings_the_cards() -> None:
    out = _run("failsAndTriesAgain")
    assert out["failed"]["held"] == 5
    assert out["failed"]["inGrid"] is False
    assert "Films" in out["failed"]["label"]
    assert out["again"] == {"failed": 1, "toasts": 1, "held": 5, "same": True}
    assert (out["refs"], out["left"], out["focus"]) == ([1, 2, 3], 0, "movie-2")


def test_try_again_goes_on_where_the_new_page_starts_with_a_heading() -> None:
    out = _run("tryAgainPastHeadings")
    assert out["bare"] == {"refs": [1, 3], "focusInGrid": True}
    assert out["led"] == {"refs": [1, 2, 3], "focus": "movie-2", "busy": None}


def test_pages_come_only_near_the_end_or_when_asked_and_far_posters_are_unloaded() -> None:
    out = _run("loadsOnlyNear")
    assert (out["first"], out["still"], out["asked"]) == ([1, 2], [1, 2], [1, 2, 3])
    assert out["next"] == "p2"
    assert out["loaded"] == [False, False, True]


def test_a_stopped_loader_takes_no_page_and_listens_to_nothing() -> None:
    assert _run("stopped") == {"before": [1, 1], "after": [0, 0], "refs": [1]}


def test_a_card_taken_out_of_the_row_does_not_shift_the_posters_kept_awake() -> None:
    assert _run("cardsTakenOut")["without"] == [True, False]


def test_the_posters_kept_awake_are_those_the_row_shows_whatever_order_they_came_in() -> None:
    assert _run("cardsInAnotherOrder") == [True, False, False]


def test_a_card_put_back_where_it_was_taken_out_still_has_its_poster() -> None:
    out = _run("cardsTakenOut")
    assert out["back"][0] is True
    assert out["far"] == [False, True]
