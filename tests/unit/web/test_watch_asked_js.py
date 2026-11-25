"""The Watch page's row of what a person asked for and has not watched, run under node against a
fake DOM."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_asked_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "TZ": "Europe/Dublin"},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_each_title_says_where_it_is_and_one_that_just_came_is_marked() -> None:
    out = _run("drawn")
    assert out["lines"] == [
        "Arrived today",
        "Arrived yesterday",
        "Arrived 3 days ago",
        "Ready to watch",
        "Arrived 2 days ago",
        "Started",
        "Almost ready",
        "Downloading",
        "Looking for it",
        "Coming 14 December",
        "Coming 1 Mar 2027",
        "Not out yet",
    ]
    assert out["states"][:6] == ["new", "new", "new", "ready", "new", "ready"]
    # One that is leaving has that badge in its place.
    assert out["news"] == [True, True, True, *[False] * 9]
    assert out["leaves"] == ["Dec14", "Mar1"]
    assert (out["skeleton"], out["before"]) == (False, "next")
    assert (out["title"], out["line"]) == ("You asked for these", "and haven’t watched them yet")


def test_a_day_is_a_day_of_the_persons_own_calendar() -> None:
    assert _run("late_evening")["lines"] == ["Arrived yesterday"]


def test_a_title_and_a_date_are_never_markup() -> None:
    out = _run("text")
    assert (out["tags"], out["title"]) == (0, "<img src=x onerror=alert(1)>")
    assert out["lines"] == ["Arrived today", "Not out yet"]


def test_with_nothing_to_show_there_is_no_row_and_no_hole() -> None:
    assert _run("nothing") == {"row": False, "skeleton": False}


def test_a_row_that_did_not_load_leaves_no_hole_and_comes_when_asked_again() -> None:
    out = _run("failed")
    assert out["at"] == {"row": False, "skeleton": False}
    assert out["keys"] == ["movie-1"]


def test_a_title_leaves_once_watched_after_a_moment_and_comes_back_with_an_undo() -> None:
    out = _run("watched")
    assert out["shown"] == ["movie-1", "movie-2"]
    assert (out["left"], out["keys"]) == (["movie-1"], ["movie-2"])
    assert out["none"] is False
    assert out["back"] == ["movie-2"]


def test_a_card_just_marked_shows_its_mark_before_it_goes() -> None:
    assert _run("marked")["gets"] == [1, 2]


def test_a_mark_or_an_undo_of_the_persons_own_waits_for_no_earlier_asking() -> None:
    out = _run("own_marks")
    assert out["keys"] == [["movie-2", "movie-3"], ["movie-3"], ["movie-2", "movie-3"]]
    # What else calls for a look still waits.
    assert out["others"] == [0, 1]


def test_an_answer_from_before_an_undo_does_not_take_the_card_away() -> None:
    stays = [["movie-1", "movie-2"]] * 2
    assert _run("undone_in_flight") == {"early": stays, "late": stays}


def test_a_title_that_comes_while_the_page_is_open_goes_first_lit_and_is_said() -> None:
    out = _run("arrives")
    assert (out["gets"], out["keys"], out["lit"]) == (2, ["movie-2", "movie-1"], ["movie-2"])
    assert out["said"] == "Film 2 is ready to watch."
    assert out["after"] == [0, 0]
    # Nothing is on its way any more, so nobody asks.
    assert out["later"] == 2


def test_under_reduced_motion_it_comes_without_the_light() -> None:
    assert _run("calm") == {"lines": ["Arrived today"], "lit": 0}


def test_a_page_nobody_looks_at_does_not_ask() -> None:
    assert _run("hidden")["gets"] == 1


def test_what_is_on_its_way_stays_while_a_service_does_not_answer() -> None:
    assert _run("unanswered")["keys"] == ["movie-1", "movie-2"]


def test_a_card_that_did_not_change_is_left_alone_with_the_focus_in_it() -> None:
    out = _run("unchanged")
    assert (out["same"], out["focused"]) == (True, True)
    assert out["keys"] == ["movie-1", "movie-2", "movie-3"]
    # Told of a change again and again, it asks once, and not at once.
    assert (out["gets"], out["lit"]) == ([1, 2], 0)


def test_the_row_has_a_limit_and_its_grid_has_them_all() -> None:
    assert _run("many") == {
        "row": [20, 20],
        "more": True,
        "said": "27",
        "grid": 27,
        "total": 27,
        "next": None,
    }


def test_a_title_in_continue_watching_is_not_repeated_and_not_counted() -> None:
    out = _run("continued")
    assert (out["keys"], out["total"], out["grid"]) == (["movie-1"], "1", 1)
    # Dismissed there, it comes here; with its Undo it goes again at once.
    assert out["dismissed"] == [["movie-1", "movie-2"], ["Arrived today", "Started"]]
    assert out["undone"] == [["movie-1"], "1", 2]


def test_one_marked_as_watched_in_continue_watching_never_shows_here() -> None:
    assert _run("continued_mark") == {"early": ["movie-1"], "keys": ["movie-1"]}


def test_with_all_of_it_in_continue_watching_there_is_no_row() -> None:
    out = _run("all_continued")
    assert (out["row"], out["skeleton"], out["grid"]) == (False, False, 0)
    assert (out["alone"], out["left"]) == (["movie-1"], ["movie-1"])


def test_a_grid_opened_before_the_row_is_there_waits_for_it() -> None:
    out = _run("grid_first")
    assert (out["first"], out["then"], out["next"], out["gets"]) == (0, 2, None, 1)


def test_a_grid_that_did_not_load_says_so_and_can_be_tried_again() -> None:
    assert _run("grid_failed") == {"failed": True, "then": 1}


def test_a_row_whose_titles_are_all_replaced_stays() -> None:
    assert _run("replaced") == {"keys": ["movie-2"], "same": True, "left": ["movie-1"]}


def test_a_mark_made_while_an_answer_is_on_its_way_is_not_lost() -> None:
    out = _run("in_flight")
    late = {"stale": ["movie-1", "movie-2"], "keys": ["movie-2"], "gets": 3}
    assert out == {"soon": late, "late": late}


def test_the_row_s_clip_leaves_room_for_an_arrival_s_glow_and_moves_nothing() -> None:
    css = (
        Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch_asked.css"
    ).read_text(encoding="utf-8")
    room = re.search(
        r"\.asked-row \.watch-row__scroll \{ padding-top: (\d+)px; margin-top: -(\d+)px; \}", css
    )
    glow = re.search(r"box-shadow: 0 0 (\d+)px", css)
    assert room
    assert glow
    assert room[1] == room[2]
    assert int(glow[1]) < int(room[1])
