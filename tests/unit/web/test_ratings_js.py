"""The set of four thumbs a person rates a title with, run under node against a fake DOM and a
server whose answers come when the scenario says."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

HARNESS = Path(__file__).parent / "js" / "ratings_harness.js"


def _run(scenario: str) -> Any:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_second_press_on_the_rating_shown_takes_it_back() -> None:
    out = _run("back")
    assert (out["first"]["on"], out["first"]["line"]) == ([], "Only you see this.")
    assert (out["before"]["on"], out["before"]["dim"]) == ([2], True)
    # At once, before the server has answered.
    assert (out["pending"]["on"], out["pending"]["dim"]) == ([], False)
    assert out["after"]["line"] == "Only you see this."
    assert out["log"] == ["GET ?refs=movie-1", "DELETE /movie-1"]
    assert [(e["phase"], e["level"], e["was"]) for e in out["events"]] == [
        ("read", 1, "unknown"),
        ("pending", None, 1),
        ("saved", None, 1),
    ]


def test_each_place_says_the_approved_words_for_each_level_and_for_none() -> None:
    said = [
        "Strong no. Gone from your picks.",
        "Not for me. Fewer like this.",
        "I like it. More like this.",
        "I love it. Counts most in your picks.",
    ]
    once = "Only you see this. You won’t be asked again."
    assert _run("levels") == {
        "plain": ["Only you see this.", *said, "Only you see this."],
        "grid": [once, *said, once],
        "strip": ["Only you see your rating.", *said, "Rating removed."],
        "menu": [
            "Rate it. Only you see this.",
            *["Strong no", "Not for me", "I like it", "I love it"],
            "Rate it. Only you see this.",
        ],
    }


def test_a_thumb_pops_when_it_rates_and_not_when_it_is_taken_back() -> None:
    assert _run("pop") == {"on": [False, False, False, True], "off": [False] * 4}


def test_a_rating_that_was_not_saved_goes_back_everywhere_and_says_so() -> None:
    out = _run("fails")
    assert [s["on"] for s in out["pending"]] == [[3], [3]]
    for shown in out["failed"]:
        assert (shown["on"], shown["bad"]) == ([1], True)
        assert shown["line"] == "Your rating wasn’t saved. Try again."
    assert (out["last"]["phase"], out["last"]["level"]) == ("failed", -1)
    assert (out["again"]["on"], out["again"]["bad"]) == ([3], False)


def test_only_the_set_that_was_pressed_is_read_out() -> None:
    assert _run("fails")["live"] == ["polite", "off"]


def test_quick_presses_are_sent_one_at_a_time_and_the_last_one_wins() -> None:
    out = _run("quick")
    assert out["out1"] == ["PUT /movie-1 2"]
    assert out["out2"] == ["PUT /movie-1 2", "PUT /movie-1 -2"]
    # A failure that a later press has overtaken shows nothing.
    assert out["log"][2:] == ["PUT /movie-1 -1", "PUT /movie-1 1"]
    assert (out["look"]["on"], out["look"]["bad"]) == ([2], False)
    assert out["saved"] == [["saved", -2, None], ["saved", 1, -2]]


def test_a_double_press_ends_with_no_rating_on_the_page_and_the_server() -> None:
    out = _run("double")
    assert out["log"] == ["PUT /movie-1 2", "DELETE /movie-1"]
    assert out["look"]["on"] == []


def test_the_levels_of_a_page_are_asked_for_together_and_once() -> None:
    out = _run("batch")
    assert out["first"] == "GET ?refs=movie-0,movie-1,movie-2,show-9"
    assert out["log"][1:] == [["GET ", 100], ["GET ", 50]]
    assert out["got"] == [2, None, -2, "unknown"]


def test_a_title_that_cannot_be_rated_has_no_thumbs() -> None:
    assert _run("batch")["hidden"] is True


def test_a_title_named_another_way_elsewhere_shows_the_new_rating_too() -> None:
    out = _run("names")
    assert out["b1"] == [3]
    # Not known to be the same title: asked about again, it alone.
    assert (out["c1"], out["log"][1]) == ([3], "GET ?refs=pick-3")
    assert out["a2"] == [0]
    assert sorted(out["refs"]) == ["movie-1", "tmdb-movie-7"]


def test_a_read_from_before_a_save_does_not_undo_it() -> None:
    out = _run("late")
    assert out["order"] == ["GET ?refs=movie-1,pick-3", "PUT /movie-1 2"]
    assert (out["look"], out["b"]) == ([3], [3])
    assert out["log"][2:] == ["GET ?refs=pick-3"]


def test_a_set_mounted_after_a_failed_save_does_not_say_it_failed() -> None:
    out = _run("failedBefore")
    assert (out["later"]["line"], out["later"]["bad"]) == ("Rate it. Only you see this.", False)
    assert out["set"]["bad"] is True
    # A refusal is the server's word that nothing changed: it is not asked again.
    assert out["log"] == []


def test_a_save_whose_answer_is_lost_is_read_again() -> None:
    out = _run("lost")
    assert out["held"]["asked"] == out["not"]["asked"] == ["GET ?refs=movie-1"]
    assert (out["not"]["look"]["on"], out["not"]["look"]["bad"]) == ([1], True)
    # Saved after all, so it no longer says it was not.
    assert (out["held"]["look"]["on"], out["held"]["look"]["bad"]) == ([3], False)


def test_the_last_known_level_is_answered_while_it_is_asked_again() -> None:
    assert _run("lost")["held"].get("meanwhile") == -1


def test_a_level_that_could_not_be_read_again_stays_known_and_is_asked_at_the_next_read() -> None:
    out = _run("offline")
    assert out.get("level") == -1
    assert (out["log"], out["then"]) == (["GET ?refs=movie-1"], 2)


def test_a_lost_answer_is_read_again_though_a_later_press_was_refused() -> None:
    assert _run("lostThenRefused") == ["GET ?refs=movie-1"]


def test_a_press_still_on_its_way_is_not_hidden_by_the_same_title_saved_elsewhere() -> None:
    assert _run("twins")["saved"] == {
        "between": [0],
        "end": [[0], [0]],
        "next": [[1], [1]],
    }


def test_a_refused_press_shows_what_the_same_title_was_saved_as_elsewhere() -> None:
    assert _run("twins")["refused"] == {
        "between": [0],
        "end": [[3], [3]],
        "next": [[1], [1]],
    }


def test_a_refused_press_is_read_again_while_the_same_title_is_being_saved_elsewhere() -> None:
    out = _run("twinsBusy")
    assert out["asked"] == ["PUT /movie-1 -1", "GET ?refs=tmdb-movie-7,movie-1"]
    assert out["end"] == [[1], [1]]


def test_a_read_that_lands_during_a_save_shows_only_if_the_save_is_refused() -> None:
    assert _run("readDuringSave") == {"during": [3], "after": [2]}


def test_a_press_before_the_read_answers_is_told_the_level_the_server_replaced() -> None:
    out = _run("unread")
    assert out["saved"][:2] == [["pending", -2, "unknown"], ["saved", -2, 2]]
    assert out["noRead"][-1] == ["saved", -2, 2]


def test_quick_presses_are_told_the_level_before_the_first_of_them() -> None:
    quick = [e for e in _run("unread")["quick"] if e[0] != "read"]
    assert quick == [
        ["pending", -2, "unknown"],
        ["pending", 1, "unknown"],
        ["pending", -2, "unknown"],
        ["pending", -1, "unknown"],
        ["saved", -1, 2],
    ]


def test_a_lost_save_read_again_is_never_told_as_the_level_before() -> None:
    out = _run("unread")
    assert [e[0] for e in out["lost"]] == ["pending", "failed", "read"]
    assert [e[2] for e in out["lost"]] == ["unknown"] * 3
    # The replaced level is the lost save's own: what was before it is not known.
    assert out["lostAgain"][-1] == ["saved", -2, "unknown"]
    assert out["lostNotHeld"][-1] == ["saved", -2, 2]


def test_a_level_known_before_a_lost_save_is_still_the_level_before_it() -> None:
    assert _run("knownLost") == [
        ["read", 1, "unknown"],
        ["pending", -2, 1],
        ["failed", 1, 1],
        ["pending", -2, 1],
        ["saved", -2, 1],
    ]


def test_a_save_after_one_that_was_lost_is_told_the_last_saved_level() -> None:
    assert _run("laterLost") == ["saved", -2, 2]


def test_a_lost_save_that_was_read_as_stored_is_the_level_before_the_next() -> None:
    assert _run("lostThenRead") == ["saved", 2, -2]


def test_an_undo_to_a_level_that_is_not_known_changes_nothing() -> None:
    assert _run("undoUnknown")["log"] == []


def test_a_set_that_is_gone_is_not_read_again_after_a_save() -> None:
    assert _run("destroyed") == ["PUT /movie-1 2"]


def test_a_page_the_browser_kept_reads_its_ratings_again() -> None:
    assert _run("kept")["on"] == [2]


def test_a_title_with_no_match_says_why_instead_of_to_try_again() -> None:
    assert _run("unmatched") == {
        "on": [],
        "dim": False,
        "line": "No match.",
        "bad": True,
        "hidden": False,
    }


def test_a_reader_gets_a_group_of_pressed_buttons_and_in_a_menu_checked_items() -> None:
    out = _run("roles")
    assert out["plain"] == [
        *["group", "Your rating", None, "Strong no"],
        *["false", None, None, "status"],
    ]
    assert out["menu"] == [
        *["group", "Your rating", "menuitemcheckbox", "Strong no"],
        *[None, "false", "-1", "status"],
    ]


def test_a_rating_set_from_elsewhere_shows_in_the_sets_still_there() -> None:
    out = _run("set")
    assert (out["a"], out["gone"]) == ([0], [])
    assert out["log"] == ["PUT /movie-1 -2"]
    assert (out["event"]["phase"], out["event"]["level"]) == ("pending", -2)
