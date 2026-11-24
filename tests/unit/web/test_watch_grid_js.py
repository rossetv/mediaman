"""The Watch page's grid of posters and the layer a row grows into (watch/grid.js), run under node
against a fake DOM with a small layout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_grid_harness.js"

OPEN = {"browse": False, "all": True, "moving": False, "open": True}
ROWS = {"browse": True, "all": False, "moving": False, "open": False}


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _state(out: dict) -> dict:
    return {k: out[k] for k in OPEN}


# ── The grid ──


def test_pages_load_to_the_end_behind_placeholders_that_finish_the_line_and_add_two() -> None:
    out = _run("loadsToTheEnd")
    assert out["waiting"] == {"skels": 3 + 10, "busy": "true"}
    assert out["nums"] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 40, 41]
    assert out["asked"] == ["p0", "p1"]
    assert (out["left"], out["tail"]) == (0, "That’s everything for now.")
    assert out["say"] == "2 more loaded. That is everything."


def test_a_failed_page_holds_still_and_try_again_goes_on_from_the_first_new_card() -> None:
    out = _run("failsAndComesBack")
    assert out["failed"] == {
        "text": "Couldn’t load more titles.Try again",
        "skels": 12,
        "still": True,
    }
    assert (out["nums"], out["skels"], out["still"], out["alert"]) == (
        [1, 2, 3, 4],
        0,
        False,
        False,
    )
    assert out["focus"] == "movie-4"


def test_the_end_says_how_many_and_an_empty_grid_says_why_with_a_link_inside_the_site() -> None:
    out = _run("ends")
    assert (out["many"], out["one"]) == ("That’s all 2.", "That’s the only one.")
    assert out["endless"] == "That’s everything for now."
    assert out["empty"] == "Nothing in Action yetTry another.Back"
    assert out["href"] == "/watch"


def test_other_contents_keep_the_posters_that_stay_and_drop_a_page_still_on_its_way() -> None:
    out = _run("otherContentsDropTheOldPages")
    assert out["nums"] == [3, 9, 1]
    assert out["sameNode"] is True
    assert (out["tail"], out["skels"]) == ("That’s all 3.", 0)


def test_other_contents_travel_from_where_they_were_and_leave_nothing_behind() -> None:
    out = _run("otherContentsTravel")
    assert out["said"] == [
        ["movie-2", "none", 1],
        ["movie-3", "translate(400px, 0px)", None],
        ["movie-1", "translate(-200px, 0px)", None],
        ["movie-4", "scale(.94)", 0],
    ]
    assert (out["gone"], out["goneAfter"], out["flyers"]) == (1, 0, 0)


def test_arrow_keys_go_by_card_and_by_line_to_the_nearest_column() -> None:
    out = _run("arrowKeys")
    assert out["path"] == ["3", "8", "12", "12", "7", "6"]
    assert out["tab"] is False
    assert out["short"] == "7"


def test_a_jump_loads_until_its_line_is_there_and_gives_in_to_the_persons_scroll() -> None:
    out = _run("jumpLoadsUntilThere")
    assert (out["before"], out["asked"]) == (1, 2)
    assert out["taken"] == 3
    assert out["scrolled"] is True


# ── The layer ──


def test_opening_shows_copies_of_the_rows_cards_and_focuses_the_heading() -> None:
    out = _run("opens")
    assert _state(out) == OPEN
    assert out["refs"] == list(range(1, 13))
    assert (out["row"], out["far"]) == (12, "/p/12")
    assert out["focus"] == "watch-all-title"
    assert out["say"] == "Movies, 12 titles, as a grid. Press Escape to go back."
    assert (out["count"], out["sort"]) == ("12 titles", False)


def test_closing_puts_scroll_the_rows_place_and_focus_back() -> None:
    out = _run("closesWhereItWas")
    assert _state(out["same"]) == ROWS
    assert (out["same"]["y"], out["same"]["left"], out["same"]["focus"]) == (90, 300, "movie-4")
    assert (out["same"]["say"], out["same"]["cards"]) == ("Back to all rows.", 0)
    assert out["onCard"] == "movie-5"
    assert out["offScreen"] != "movie-12"


def test_from_the_middle_of_a_row_the_grid_opens_on_that_cards_line() -> None:
    # 8px under a header that ends at 114
    assert _run("midRowOpensOnItsLine")["top"] == 122


def test_a_shared_link_opens_with_no_flight_and_closes_to_its_row() -> None:
    out = _run("fromALink")
    assert out["opened"] == OPEN
    assert _state(out) == ROWS
    assert (out["mid"], out["focus"]) == (400, "A")


def test_a_grid_the_server_drew_is_taken_over_and_not_used_again() -> None:
    out = _run("serverDrawn")
    assert out["drawn"] == [50, 51, 52]
    assert out["after"] == list(range(1, 13))


def test_opening_and_closing_move_transform_and_opacity_only_and_land_clean() -> None:
    out = _run("movesAndLands")
    assert out["mid"] == {
        "browse": True,
        "all": True,
        "moving": True,
        "open": True,
        "pinned": True,
        "inert": True,
    }
    assert out["afterOne"] is True
    assert out["from"].startswith("translate(0px, -24px) scale(0.73")
    assert out["props"] == ["opacity", "transform"]
    assert out["landed"] == {**OPEN, "left": 0}
    assert (out["closing"]["pinned"], out["closing"]["open"]) == (True, False)
    assert out["closed"] == {**ROWS, "left": 0, "cards": 0}


def test_turning_round_midway_starts_from_where_things_are_at_once() -> None:
    out = _run("turnsRoundFromWhereItIs")
    assert out["from"] == "matrix(0.9, 0, 0, 0.9, 12, 34)"
    assert (out["delays"], out["slowest"]) == ([0], 320)
    assert out["cancelled"] is True
    assert out["after"]["moving"] is True
    assert out["again"] == "translate(0px, 0px) scale(1)"
    assert (out["cards"], out["landed"], out["left"]) == (12, OPEN, 0)


def test_a_resize_midway_lands_at_once() -> None:
    assert _run("resizedMidway") == {**OPEN, "left": 0}


def test_reduced_motion_opens_sorts_and_closes_with_nothing_moving() -> None:
    assert _run("reducedMotion") == {"open": OPEN, "closed": ROWS, "anims": 0}


def test_escape_goes_back_only_when_the_grid_is_on_top_and_nothing_else_took_it() -> None:
    assert _run("escape") == {"under": 0, "eaten": 0, "own": 1, "closed": 1}


def test_the_back_link_goes_back_in_place_but_still_opens_a_new_tab() -> None:
    assert _run("backLink") == {"newTab": [0, False], "plain": [1, True]}


def test_sorting_shows_the_last_order_asked_for_and_a_failure_puts_the_name_back() -> None:
    out = _run("sorts")
    assert out["waiting"] == ["Sort: A to Z", "true"]
    assert out["late"] == 12
    assert out["done"] == {
        "refs": [6, 2],
        "label": "Sort: Newest release",
        "count": "2 titles",
        "busy": None,
        "say": "Movies, Sort: Newest release.",
    }
    assert out["chosen"] == ["year"]
    assert out["failed"] == ["Sort: Newest release", [6, 2], 1, None]


def test_an_order_that_arrives_while_the_grid_leaves_changes_nothing() -> None:
    out = _run("sortAnswersAfterLeaving")
    assert (out["during"], out["label"], out["chosen"]) == (12, "Sort: Recently added", [])
    assert (_state(out), out["left"]) == (ROWS, 0)


def test_leaving_for_another_view_shows_neither_rows_nor_grid() -> None:
    out = _run("leaves")
    assert (out["browse"], out["all"], out["moving"], out["left"]) == (False, False, False, 0)


def test_an_empty_category_has_no_sort_and_no_count() -> None:
    out = _run("nothingToSort")
    assert (out["sort"], out["count"]) == (False, "")
    assert out["text"].startswith("Nothing in Movies yet")


def test_closing_to_a_row_that_has_gone_still_lands() -> None:
    assert _run("rowThatIsGone") == {**ROWS, "left": 0, "focusInPage": True}


def test_nothing_loads_while_it_moves_nor_once_it_is_closed() -> None:
    assert _run("loadsOnlyWhenStill") == {"moving": 0, "landed": 1, "closed": 1, "heard": 0}


def test_a_stopped_grid_listens_to_nothing_more() -> None:
    out = _run("stopped")
    assert out["grew"] == [True] * 5
    assert out["after"][:4] == [0] * 4
    assert out["after"][4] <= 1  # the loader's own


def test_a_grid_on_its_way_out_goes_with_the_page_as_it_is_scrolled() -> None:
    assert _run("scrollsWhileClosing") == {"by": -70}


def test_a_grid_opened_over_hidden_rows_does_not_close_to_an_earlier_place() -> None:
    assert _run("placeIsForgotten") == {"y": 140, "focus": "A"}


def test_a_grid_left_for_another_view_and_opened_again_closes_to_where_the_rows_were() -> None:
    assert _run("openedAgain") == {"left": 0, "y": 90, "focus": "movie-4"}


def test_a_jump_gives_up_when_its_page_fails_or_is_late_and_for_a_key_that_scrolls() -> None:
    out = _run("jumpGivesUp")
    assert (out["there"], out["typed"]) == (4, 4)
    # The page it was waiting for, once more after Try again where it failed.
    assert (out["failed"], out["key"], out["slow"]) == (2, 1, 1)


def test_opening_closes_a_preview_and_copies_no_card_as_previewed() -> None:
    assert _run("previewGoes") == {"closed": 1, "copies": 0}


def test_a_card_taken_out_lets_the_rest_close_up_and_comes_back_to_its_place() -> None:
    out = _run("takesOut")
    assert (out["notHere"], out["ghost"], out["moved"]) == (None, True, 11)
    assert out["gone"] == {"inert": True, "count": "11 titles", "focus": "movie-3"}
    assert (out["refs"], out["count"], out["back"]) == ([1, 2, 3], "12 titles", "movie-2")
    assert out["left"] == 0


def test_a_card_undone_after_it_faded_out_of_the_grid_is_seen_again() -> None:
    assert _run("backFromAFade") == {"off": True, "held": False, "back": 2}


def test_a_card_back_in_its_row_comes_back_to_a_grid_that_was_filled_without_it() -> None:
    out = _run("bringsBack")
    every = list(range(1, 13))
    assert out["sorted"] == {
        "refs": every,
        "count": "12 titles",
        "sort": True,
        "stale": None,
        "brought": 2,
        "y": 400,
        "twice": None,
    }
    assert out["lateUndo"] is None
    assert out["reopened"] == {"refs": every, "count": "12 titles", "sort": True}
    assert out["emptied"] == {
        "empty": {"refs": [], "count": "", "sort": False},
        "other": 0,
        "back": {"refs": [1], "count": "1 title", "sort": True},
    }


def test_each_season_of_one_show_comes_back_as_itself() -> None:
    assert _run("bringsSeasons") == {
        "marks": ["season/1", "season/2", "movie/2"],
        "count": "3 titles",
    }


def test_a_card_comes_back_to_a_grid_that_is_opening_and_not_to_one_that_closes() -> None:
    out = _run("bringsWhileMoving")
    assert out["opening"] == {**OPEN, "n": 12, "left": 0}
    assert out["closing"] == {**ROWS, "n": 0, "left": 0}


def test_closing_to_a_row_that_went_with_its_last_card_gives_the_search_the_focus() -> None:
    assert _run("closesWithNoRow") == "watch-search"
