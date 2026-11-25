"""The Watch page's grid of posters and the layer a row grows into (watch/grid.js), run under node
against a fake DOM with a small layout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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


def test_a_card_is_copied_with_its_levels_folded_and_one_tab_stop_for_its_buttons() -> None:
    assert _run("copiedTools") == {"closed": True, "stops": [None, "-1", "-1"]}


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


def test_cards_taken_from_a_row_are_in_the_tab_order_of_the_grid() -> None:
    assert _run("keysOfItsOwn") == {"row": ["-1"], "grid": [None]}


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


def test_the_first_page_that_came_during_a_sort_is_put_in_once_the_sort_has_failed() -> None:
    out = _run("sortWhileTheFirstPageComes")
    assert out["fails"]["refs"] >= 20
    assert out["fails"]["stood"] == [20]  # told once its cards stand
    assert (out["fails"]["before"], out["fails"]["label"]) == (12, "Sort: Recently added")
    # The order asked for has the last word.
    assert (out["comes"]["refs"], out["comes"]["label"]) == (2, "Sort: A to Z")
    # Neither came: what stands stays, and the rest is asked for as a page.
    assert out["bothFail"]["refs"] == 13


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


def test_the_last_pick_of_a_date_takes_its_line_along_and_the_first_back_has_it_again() -> None:
    out = _run("datedOut")
    assert out["one"] == ["b1", 1, "b2", 3]
    assert out["gone"]["seq"] == []
    assert out["loaded"]["seq"] == ["b3", 4]
    # Back in another order than they went, and before what came later.
    assert out["first"] == ["b1", 1, "b2", 3, "b3", 4]
    assert out["back"]["seq"] == ["b1", 1, 2, "b2", 3, "b3", 4]


def test_a_date_with_nothing_under_it_any_more_is_not_there_to_jump_to() -> None:
    out = _run("datedOut")
    assert out["loaded"]["jumps"] == ["b3"]
    assert out["back"]["jumps"] == ["b1", "b2", "b3"]


def test_the_date_named_is_not_one_that_has_gone_even_if_it_was_jumped_to() -> None:
    assert _run("datedOut")["gone"]["label"] == "Jump to: b2"


@pytest.mark.parametrize(
    ("case", "focus", "kept"),
    [
        ("onButton", "watch-all-title", False),
        ("inMenu", "watch-all-title", False),
        ("elsewhere", "watch-all__back", True),
    ],
)
def test_with_no_date_to_jump_to_the_button_goes_and_comes_back_and_no_focus_is_lost(
    case: str, focus: str, kept: bool
) -> None:
    assert _run("datedOut")[case] == {
        "none": [],
        "hidden": True,
        "menu": False,
        "focus": focus,
        "kept": kept,
        "back": False,
    }


def test_a_dates_line_fades_out_with_its_last_pick_and_stands_as_it_did_once_back() -> None:
    out = _run("datedOut")
    assert (out["fades"], out["faded"]) == (True, False)
    assert out["seen"] == [["b1", 1, 2, "b2", 3], False, "grid-group", ""]


@pytest.mark.parametrize(
    ("case", "seq", "undone"),
    [
        ("middle", ["a", 1, 2, 3, "b", 4, "c", 5, "d", 6], []),
        ("firstOfDate", ["a", 1, 2, 3, "b", 4], []),
        ("aloneFirst", ["a", 1, "b", 4], []),
        ("aloneBetween", ["a", 1, 2, 3, "b", 4, "c", 5], []),
        ("aloneLast", ["a", 1, 2, 3, "c", 5], ["a", 1, 2, 3, "b", 4, "c", 5]),
        ("aloneAfterTaken", ["a", 1, 2, "b", 4], ["a", 1, 2, 3, "b", 4, "c", 5]),
        ("dateTaken", ["a", 2, "b", 4], ["a", 1, 2, 3, "b", 4]),
        ("dateTakenBack", ["a", 2, "b", 4], ["a", 1, 2, 3, "b", 4]),
        ("onlyMarkOfDate", ["a", 9, "b", 1, "c", 5], ["a", 9, "b", 1, 2, "c", 5]),
        ("afterAMark", ["a", 1, "b", 4, "c", 5], ["a", 1, 3, "b", 4, "c", 5]),
        ("allOut", ["a", 1], ["a", 1, "b", 2]),
    ],
)
def test_a_pick_back_in_its_row_stands_under_its_date_in_the_grid_opened_without_it(
    case: str, seq: list[object], undone: list[object]
) -> None:
    assert _run("bringsDated")[case] == [True, seq, undone or seq]


def test_a_pick_of_a_date_whose_page_is_to_come_is_left_to_that_page() -> None:
    seq = ["a", 1, 2, 3]
    assert _run("bringsDated")["toCome"] == [False, seq, seq]


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


def test_leaving_all_titles_gives_the_focus_to_its_link_and_never_to_the_field_by_touch() -> None:
    assert _run("closesToTheLink") == {
        "mouse": "watch-all-link",
        "touch": "watch-all-link",
        "search": "watch-search",
        "searchTouch": "watch-all-link",
        "bare": "main-content",
        "bareTouch": "main-content",
        # Out of sight, the next Tab would jump to it.
        "below": "main-content",
    }


def test_all_titles_opened_by_its_address_goes_back_to_where_its_link_stands() -> None:
    out = _run("backToTheLink")
    assert out["still"] == [3000 - (800 - 20) / 2, "watch-all-link"]
    assert out["moving"] == out["still"]


def test_the_open_grid_asked_for_again_stays_as_it_is_or_takes_the_order_asked_for() -> None:
    out = _run("opensAgain")
    assert out["same"] == [True, 700]
    assert out["other"] == ["Sort: Recently added", list(range(1, 13))]
    # A line to jump to says where the screen stands: nothing to follow.
    assert out["jump"] == "Sort: Recently added"
    assert out["midway"] == ["Sort: Recently added", list(range(1, 13)), True, 0]


def test_a_page_made_short_for_a_moment_moves_no_layer_from_where_it_stood() -> None:
    out = _run("scrolledAndTurned")
    assert (out["rows"], out["grid"]) == (20, 74)
    assert out["opened"] == [20, 50, "translate(0px, -94px) scale(0.7368421052631579)"]
    assert out["closing"][:2] == [74, 120]
    assert out["closing"][2].startswith("translate(0px, -54px)")
    assert out["closing"][3] == ""


def test_what_loads_as_a_grid_opens_does_not_cut_off_what_it_says_of_itself() -> None:
    opened, *early, later = _run("opensAndLoads")
    assert opened == "Movies, 12 titles, as a grid. Press Escape to go back."
    assert early == [opened, opened]
    assert later == "1 more loaded. That is everything."


def test_forward_leaves_a_grid_scrolled_on_into_to_grow_back_and_back_puts_its_rows_back() -> None:
    out = _run("throughTheHistory")
    assert (out["deep"]["placed"], out["deep"]["moving"]) == (False, False)
    assert out["shallow"] == {"placed": True, "moving": True, "y": 60}
    assert out["still"]["placed"] is False
    assert out["closed"] == {"y": 777, "left": 450}


def test_a_title_with_no_heading_to_go_to_fades_with_the_rest() -> None:
    assert _run("titleAlone") == {"opens": [0, 1], "closes": [1, 0]}


def test_a_grid_that_opens_on_nothing_asks_at_once_and_holds_the_place_of_what_comes() -> None:
    out = _run("firstPage")
    assert out["asked"] == [""]
    # Enough to fill the screen, 800 px high, in five columns, from the first frame on.
    assert out["first"] == [15, 0]
    assert out["lead"] == [0]
    assert out["count"] == "\u00a0"
    assert out["moving"] == [15, 0, 0]
    assert out["rested"] == [0, 3]
    # Nothing goes blank in between.
    assert (out["boxFades"], out["wordsFade"], out["toolsFade"]) == (False, True, True)
    # What its titles have had changed since is read again once they stand there.
    assert out["placed"] == 1
