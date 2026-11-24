"""The Watch page's address and Back (watch/address.js), run under node against a fake history."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_address_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_back_closes_what_is_open_from_the_top_and_forward_opens_it_again() -> None:
    out = _run("nestedBackAndForward")
    assert (out["pushed"], out["top"]) == ([], "trailer")
    assert out["back"] == [
        ["/watch?all=movies#title=movie-1", "close trailer pop"],
        ["/watch?all=movies", "close title pop"],
        ["/watch", "close grid pop"],
    ]
    assert [step[1] for step in out["forward"]] == [
        "open grid movies pop",
        "open title movie-1 pop",
        "open trailer on pop",
    ]
    assert out["all"] == ["close trailer pop", "close title pop", "close grid pop"]


def test_closing_a_layer_steps_past_what_stands_on_it_at_once_and_leaves_what_is_under() -> None:
    out = _run("closeStepsPastWhatStandsOnIt")
    assert out["calls"] == ["go -2"]
    assert out["at"] == "/watch?q=x&all=movies"
    assert out["log"] == ["close trailer pop", "close title pop"]


def test_a_shared_link_opens_bottom_up_and_closes_by_losing_only_its_own_part() -> None:
    out = _run("sharedLink")
    assert out["opened"] == ["open grid movies az start", "open title movie-1 start"]
    assert out["title"] == ["/watch?all=movies&sort=az", "close title replace"]
    assert out["grid"] == ["/watch", "close grid replace"]
    assert (out["entries"], out["calls"]) == (1, [])


def test_a_shared_title_with_a_trailer_over_it_closes_both_and_stays_on_its_entry() -> None:
    out = _run("sharedTitleWithATrailerOverIt")
    assert (out["at"], out["index"]) == ("/watch", 0)
    assert out["log"] == ["close trailer pop", "close title pop"]


def test_closing_a_search_over_the_grid_keeps_the_grids_entry() -> None:
    out = _run("closingWhatWasNotPushedLeavesTheEntryUnder")
    assert (out["at"], out["calls"], out["top"]) == ("/watch?all=movies", [], "grid")
    assert out["log"] == ["close search replace"]


def test_filters_change_the_entry_and_back_takes_one_view_where_close_takes_all() -> None:
    out = _run("filtersReplaceAndViewsPush")
    assert (out["entries"], out["own"]) == (2, [])
    assert out["one"] == [
        "/watch?genre=action&type=movie&decade=1990",
        "open explore action movie 1990 pop",
    ]
    assert out["all"] == ["/watch", "close explore pop"]


def test_going_to_another_view_closes_the_one_that_was_open() -> None:
    assert _run("anotherViewClosesTheGrid") == {
        "list": ["close grid push"],
        "rows": ["close list push"],
    }


def test_after_a_reload_the_way_back_and_each_scroll_are_still_there() -> None:
    out = _run("reloadKeepsTheWayBack")
    assert out["opened"] == ["open grid movies start", "open title movie-1 start"]
    assert (out["scroll"], out["after"]) == (1500, 700)
    assert (out["calls"], out["at"]) == (["go -2"], "/watch")


def test_back_and_forward_put_scroll_and_focus_back() -> None:
    out = _run("scrollAndFocusComeBack")
    assert out == {"back": [900, "card"], "forward": 300, "restoration": "manual"}


def test_focus_that_is_already_somewhere_is_left_there() -> None:
    assert _run("focusALayerPutSomewhereStays")["focus"] == "other"


def test_a_layer_that_places_the_scroll_has_the_last_word() -> None:
    assert _run("aLayerThatPlacesTheScroll")["scroll"] == 640


def test_scroll_waits_for_a_page_that_is_still_filling_unless_the_person_scrolls() -> None:
    assert _run("aPageThatGrows") == {"left": [200, 900], "taken": [200, 50]}


def test_a_step_the_browser_never_makes_still_closes() -> None:
    out = _run("aStepThatNeverComes")
    assert out["before"] == "/watch#title=movie-1"
    assert (out["at"], out["log"]) == ("/watch", ["close title replace"])


def test_a_second_close_during_the_first_is_made_after_it() -> None:
    out = _run("twoClosesInARow")
    assert (out["at"], out["index"]) == ("/watch", 0)
    assert out["log"] == ["close trailer pop", "close title pop"]


def test_a_link_followed_in_the_page_opens_its_layer_and_closes_alone() -> None:
    out = _run("aFollowedLink")
    assert out["opened"] == ["open storage storage-deleted pop"]
    assert out["closed"] == ["/watch?all=movies", "close storage replace"]


def test_one_layer_that_fails_does_not_keep_the_others_from_the_address() -> None:
    out = _run("aLayerThatFails")
    assert out["log"][-1] == "open title movie-1 start"
    assert out["thrown"] == 1


def test_a_title_under_its_new_ref_keeps_the_trailer_over_it() -> None:
    out = _run("aTitleUnderANewRef")
    assert out["under"] == "trailer"
    assert (out["at"], out["value"]) == ("/watch#title=movie-9", "movie-9")


def test_the_same_press_twice_is_one_entry() -> None:
    assert _run("samePushTwice")["entries"] == 2


def test_addresses_that_work_today_are_read_and_kept_as_they_are() -> None:
    out = _run("todaysLinks")
    assert out["links"] == [
        ["/watch"],
        ["/watch?q=amber", "open search amber start"],
        [
            "/watch?view=list&sort=size_desc&q=amber",
            "open list view=list&sort=size_desc&q=amber start",
        ],
        ["/watch#title=movie-1", "open title movie-1 start"],
        ["/watch?all=nothing"],
        ["/watch#storage-scheduled", "open storage storage-scheduled start"],
    ]
    assert out["url"] == [
        "/watch?q=a+b#title=movie-1",
        "/watch?row=recommended",
        "/watch?row=recommended&q=a+b#storage",
    ]


def test_an_old_link_moved_before_the_start_opens_once() -> None:
    assert _run("beforeStart") == {"before": [], "after": ["open title rec-7 start"]}


def test_back_works_where_the_browser_refuses_storage() -> None:
    assert _run("noStorage")["log"] == ["close grid pop"]


def test_typing_a_search_writes_the_address_once_for_each_change() -> None:
    assert _run("typing") == {"writes": 3, "at": "/watch?all=movies&q=amb"}


def test_whatever_opens_closes_a_preview_but_the_details_that_grow_out_of_it() -> None:
    assert _run("closesThePreview") == [1, 1, 2]


def test_a_page_drawn_anew_each_time_is_let_go_once_it_is_left() -> None:
    assert _run("pagesThatGo") == {"held": 0}


def test_a_page_drawn_anew_is_let_go_while_its_entry_stands() -> None:
    assert _run("pagesThatGoOn") == {"held": 0}


def test_pages_gone_for_good_do_not_push_out_the_scroll_of_those_under_them() -> None:
    assert _run("entriesThatGo") == {"scroll": 700}


def test_the_focus_comes_back_from_under_a_link_the_browser_followed() -> None:
    assert _run("focusUnderAFollowedLink") == {"at": "/watch?all=movies", "focus": "card"}


def test_a_second_back_while_the_first_is_on_its_way_steps_no_further() -> None:
    assert _run("twoBacksInARow") == {"at": "/watch", "calls": ["go -1"]}


def test_a_link_to_the_player_carries_the_whole_address_it_was_followed_at() -> None:
    assert _run("playLinks") == [
        ["/watch/play/7", "1", "/watch?genre=action&decade=1990s#title=movie-7"],
        ["/watch/play/7", "1", "/watch?q=a%26b"],
        ["/watch/play/7", None, "/watch?all=movies"],
        ["/watch/movie/7", None, None],
    ]
