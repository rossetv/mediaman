"""The rows of the Watch page that grow into grids, run under node against a fake DOM and history."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_expand_harness.js"
BROWSE = (
    Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch_browse.css"
).read_text(encoding="utf-8")


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_row_s_heading_opens_its_grid_which_goes_on_from_the_row_and_back_returns() -> None:
    out = _run("library")
    assert out["label"] == "Movies, see all 32"
    assert (out["prevented"], out["at"]) == (True, "/?all=library&type=movies")
    assert (out["title"], out["count"], out["sort"]) == (
        "All titles",
        "32 titles",
        "Recently added",
    )
    assert (out["n"], out["last"]) == (32, ["Film 31", "Film 32"])
    assert out["calls"] == ["/api/watch/library/library/row?limit=60&type=movies"]
    assert out["filters"] == ["All40", "Movies32*", "TV6", "Anime2", "Not watched3"]
    assert out["rows"] is True  # hidden behind it
    assert out["back"] == {"at": "/", "grid": None, "rows": True, "focus": True}


def test_the_order_chosen_comes_from_the_server_and_stays_in_the_address() -> None:
    out = _run("sorted")
    assert out["labels"] == ["Recently added", "A to Z", "Newest release", "Not watched first"]
    assert out["sorted"]["at"] == "/?all=library&type=movies&sort=az"
    assert out["sorted"]["grid"]["things"] == ["Film 9", "Film 1"]
    assert out["back"] == {"at": "/", "grid": None}
    assert out["forward"] == {
        "at": "/?all=library&type=movies&sort=az",
        "things": ["Film 9", "Film 1"],
        "sort": "A to Z",
    }
    assert out["again"] == {"at": "/?all=library&type=movies", "n": 32, "first": "Film 1"}


def test_newest_release_and_not_watched_first_are_the_library_s_own_orders() -> None:
    out = _run("ordered")
    assert out["seen"] == [
        {
            "at": "/?all=library&type=movies&sort=unwatched",
            "sort": "Not watched first",
            "things": ["Film 4", "Film 6"],
        },
        {
            "at": "/?all=library&type=movies",
            "sort": "Recently added",
            "things": [f"Film {n}" for n in range(1, 33)],
        },
        {
            "at": "/?all=library&type=movies&sort=year",
            "sort": "Newest release",
            "things": ["Film 5", "Film 3", "Film 7"],
        },
    ]
    assert out["forward"][:2] == ["Film 5", "Film 3"]
    assert out["link"] == "Not watched first"
    assert out["other"] == "Last watched"


def test_a_row_all_on_the_page_is_put_in_order_there_as_the_server_would() -> None:
    out = _run("continued")
    assert out["first"]["things"][0] == "The Zebra"
    assert out["sorted"] == ["An Apple", "Éclair", "Fig", "Gnu", "Orchard", "The Zebra"]
    assert (out["at"], out["calls"]) == ("/?all=continue&sort=az", [])


def test_trending_shows_films_or_tv_alone_on_every_page_and_counts_nothing() -> None:
    out = _run("filtered")
    assert out["all"] == ["T1", "T2", "T3", "T4", "T5"]
    assert (out["count"], out["tail"]) == ("", "That’s everything for now.")
    assert (out["tv"], out["at"]) == (["T2", "T4"], "/?all=trending&sort=tv")


def test_picks_stand_under_the_date_of_their_batch_labelled_as_ai_picks() -> None:
    out = _run("picks")
    assert out["things"] == [
        "[AI picksLatest picks · 23 Nov]",
        "P1",
        "P2",
        "[AI picksPicks of 9 Nov]",
        "P3",
        "[AI picksPicks of 26 Oct]",
        "P4",
    ]
    assert out["sort"] == "Latest · 23 Nov"
    assert out["labels"] == ["Latest · 23 Nov", "9 Nov", "26 Oct"]
    assert out["pill"] == "26 Oct"


def test_a_batch_of_repeats_alone_leaves_no_date_with_nothing_under_it() -> None:
    assert _run("picksRepeated") == [
        "[AI picksLatest picks · 23 Nov]",
        "P1",
        "P2",
        "[AI picksPicks of 26 Oct]",
        "P4",
    ]


def test_a_first_batch_without_a_date_stands_under_latest_picks_alone() -> None:
    out = _run("picksOfTwoDays")
    assert out["lines"] == [
        "[AI picksLatest picks]",
        "[AI picksPicks of 9 Nov]",
        "[AI picksPicks of 26 Oct]",
    ]
    assert out["sort"] == "Latest"


def test_a_row_that_is_all_on_the_screen_offers_no_grid() -> None:
    assert _run("whole") == {
        "plain": {"href": None, "label": None, "cue": False},
        "prevented": False,
        "at": "/",
        "grid": None,
    }


def test_three_screens_of_one_row_bring_the_offer_and_another_row_takes_it_away() -> None:
    out = _run("offered")
    assert out["seen"] == [False, False, True]
    assert (out["text"], out["endless"]) == ("See all32", "See all")
    assert (out["label"], out["ways"]) == ("Movies, see all 32", 1)
    assert (out["taken"], out["again"], out["back"]) == (False, False, True)
    assert out["prevented"] is True
    assert (out["at"], out["title"], out["left"]) == (
        "/?all=library&type=movies",
        "All titles",
        False,
    )


def test_a_row_s_heading_moves_once_and_its_offer_comes_back_still() -> None:
    out = _run("calledOnce")
    still = [True, False]
    assert out["seen"] == [[True, True], [True, True], still, [False, False], still]
    assert out["other"] == [True, True]


def test_going_back_takes_the_offer_and_lets_the_heading_settle_and_a_grid_stops_it() -> None:
    assert _run("calledOff") == {
        "back": [False, True],
        "hidden": [True, False],
        "pressed": False,
        "closed": [False, False],
    }


def test_under_reduced_motion_the_offer_comes_with_nothing_left_to_move_later() -> None:
    assert _run("calledStill") == [True, False]


def test_the_room_above_the_cards_takes_none_of_the_heading_s_44px() -> None:
    head = re.search(r"^\.watch-row__head \{([^}]*)\}", BROWSE, re.M)
    assert head
    assert "position: relative" in head[1]
    assert "z-index: 1" in head[1]


def test_in_forced_colours_no_backplate_of_the_count_covers_see_all() -> None:
    forced = BROWSE[BROWSE.index("@media (forced-colors: active)") :]
    for state in ("", ":hover", ":active"):
        selector = re.escape(f".row-open[href]{state} .row-open__cue")
        cue = re.search(rf"(?<=[{{}},\s]){selector}(?:,[^{{}}]*)? \{{([^}}]*)\}}", forced)
        assert cue, state
        assert "forced-color-adjust: none" in cue[1]
        assert re.search(r"(?<!-)color: LinkText", cue[1])


def test_on_a_phone_see_all_is_seen_only_once_clear_of_the_title_and_goes_at_once() -> None:
    late = re.findall(r"([^{}]*)\{ transition-delay: \.12s; \}", BROWSE)
    assert len(late) == 2
    # Never on the words at rest, which would linger as they go.
    assert all(re.search(r":focus-visible|:hover", head) for head in late)


def test_a_row_with_nothing_more_to_see_makes_no_offer() -> None:
    assert _run("offeredWhole") == {"whole": False, "before": True, "after": False}


def test_a_heading_swells_no_further_than_the_room_beside_it() -> None:
    out = _run("room")
    assert (out["alone"], out["far"], out["under"]) == (1, 1, 1)
    # All of it: 14% of 200px, and the 18% of See all's chip in it.
    whole = 0.14 * 200 + 1.14 * 0.18 * 80
    assert 0.1 < out["near"] <= 12 / whole
    assert out["touching"] == 0
    assert 0.3 < out["edge"] <= 20 / whole


def test_the_heading_swells_14_percent_and_8_on_a_phone() -> None:
    assert re.findall(r"--row-swell: ([\d.]+);", BROWSE) == [".14", ".08"]
    assert "@media (max-width: 599.98px) {\n  .row-open { --row-swell: .08;" in BROWSE
    assert "row-swell 1.1s" in BROWSE
    assert "scale(calc(1 + var(--row-swell) * var(--row-room, 1)))" in _keyframes("row-swell")
    assert "scale(calc(1 + var(--row-chip) * var(--row-room, 1)))" in _keyframes("row-chip")


def _keyframes(name: str) -> str:
    return re.findall(rf"@keyframes {name} \{{((?:[^{{}}]*\{{[^{{}}]*\}})*)", BROWSE)[0]


def test_the_heading_moves_nothing_else_and_only_where_motion_is_welcome() -> None:
    start = BROWSE.index("@media (prefers-reduced-motion: no-preference)")
    moving = re.match(r"[^{]*\{((?:[^{}]*\{[^{}]*\})*)", BROWSE[start:])
    assert moving
    assert BROWSE.count(".is-calling") == moving.group(1).count(".is-calling") > 0
    names = set(re.findall(r"animation: ([\w-]+)", moving.group(1)))
    assert names
    for name in names:
        # Never a size or a place, which would move the row under it.
        assert set(re.findall(r"([\w-]+):", _keyframes(name))) <= {"transform", "opacity"}
    assert "infinite" not in moving.group(1)
    # The class that plays it goes with the heading's own motion, so nothing in it lasts longer.
    times = {
        sel.strip(): sum(float(t) for t in re.findall(r"([\d.]+)s\b", body))
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", moving.group(1))
    }
    assert times[".row-open.is-calling"] == max(times.values())


def test_a_click_for_a_new_tab_or_on_an_unknown_row_is_the_browser_s() -> None:
    assert _run("otherClicks") == {"tab": False, "unknown": False, "grid": None, "at": "/"}


def test_a_shared_link_keeps_the_server_s_cards_and_goes_on_after_them() -> None:
    out = _run("cold")
    assert out["open"]["things"] == ["Film 1", "Film 2", "Film 3", "Film 4", "Film 5"]
    assert out["same"] is True
    # Back from a link leads to the rows in the same entry.
    assert (out["at"], out["rows"], out["entries"]) == ("/", True, 1)


def test_a_shared_link_to_a_row_that_loads_later_fills_once_it_is_here() -> None:
    out = _run("coldLater")
    assert (out["trending"], out["asked"]) == (["T1", "T2", "T3", "T4", "T5"], 1)
    assert out["picks"][0] == "[AI picksLatest picks · 23 Nov]"
    assert out["picks"][-1] == "P4"
    assert out["sort"] == "Latest · 23 Nov"
    assert (out["tail"], out["retried"]) == ("Couldn’t load the titles.Try again", ["T7"])


def test_a_search_takes_the_page_from_the_grid_and_back_opens_it_again() -> None:
    out = _run("leftFor")
    assert out["left"] == {"grid": None, "rows": False}
    assert out["back"] == {"at": "/?all=library&type=movies", "title": "All titles"}
    assert out["browse"] == {"at": "/", "grid": None, "rows": True}


def test_an_address_that_asks_for_something_else_or_for_nothing_known_opens_no_grid() -> None:
    assert _run("odd") == {
        "search": None,
        "adminsOrder": ["All titles", "Recently added"],
        "adminsFilter": ["All titles", "Recently added"],
        "genre": None,
        "unknown": None,
        "inherited": None,
        "order": ["Continue watching", "Last watched"],
    }


def test_a_row_s_grid_keeps_nothing_of_a_category_in_its_address() -> None:
    assert _run("afterACategory") == "/?type=movies&all=library"


def test_a_card_kept_or_dismissed_in_the_grid_leaves_it_and_its_row_and_comes_back_to_both() -> (
    None
):
    out = _run("taken")
    assert out["gone"] == {
        "things": ["The Zebra", "An Apple", "Éclair", "Fig", "Gnu"],
        "count": "5 titles",
        "focus": "An Apple",
        "cards": 0,
    }
    assert out["back"]["things"][1] == "Orchard"
    assert (out["back"]["count"], out["back"]["cards"], out["left"]) == ("6 titles", 2, False)
    assert out["empty"]["things"] == []
    assert out["empty"]["tail"].startswith("Nothing is leaving soon")
    assert (out["empty"]["focus"], out["empty"]["body"]) == ("watch-all-title", True)


def test_a_card_comes_back_to_a_grid_filled_again_since_in_its_place() -> None:
    out = _run("taken")
    # The row's undo fills the grid again, and says what is shown of it.
    assert out["sorted"] == {
        "things": ["An Apple", "Éclair", "Fig", "Gnu", "Orchard", "The Zebra"],
        "count": "6 titles",
        "focus": [None, True],
    }
    assert out["reopened"] == {
        "things": ["Film 51", "Film 52"],
        "count": "2 titles",
        "focus": [None, True],
    }


def test_the_undo_of_a_keep_focuses_the_keep_that_is_shown() -> None:
    out = _run("keepUndone")
    both = ["Film 51", "Film 52"]
    grid = {"where": "grid", "keep": "51", "shown": True, "same": True}
    grid.update(things=both, kept="52")
    assert (out["inGrid"], out["fromPreview"], out["opening"]) == (grid, grid, grid)
    assert out["closed"] == {**grid, "where": "row", "same": False, "things": None}


def test_closing_to_a_row_that_now_shows_whole_focuses_its_cards() -> None:
    assert _run("closedWhole") == ["watch-row__scroll", True, None]


def test_the_page_s_own_scrolls_of_a_row_under_its_grid_bring_no_offer() -> None:
    assert _run("offerUnderGrid") == [False, False]


def test_only_a_grid_left_for_another_view_opens_again_where_it_was() -> None:
    assert _run("leftAndBack") == [[False, False], [True, True]] + [[False, False]] * 3


def test_a_row_changed_while_a_panel_holds_the_page_keeps_its_way_in_and_its_count():
    assert _run("underAPanel") == {
        "href": "/?all=continue",
        "label": "Continue watching, see all 5",
        "cue": True,
        "offer": "See all5",
    }


def test_a_filter_is_in_the_address_beside_the_order_and_pressed_again_is_off() -> None:
    out = _run("filters")
    unseen = out["unseen"]
    assert (out["prevented"], unseen["at"]) == (True, "/?all=library&type=unseen")
    assert (unseen["things"], unseen["count"]) == (["Film 2", "Film 8", "Film 3"], "3 titles")
    assert unseen["filters"][-1] == "Not watched3*"
    assert unseen["focus"] == "unseen"
    assert unseen["say"] == "All titles, Not watched, 3 titles, Sort: Recently added."
    assert out["sorted"] == {
        "at": "/?all=library&type=unseen&sort=az",
        "things": ["Film 3", "Film 2"],
    }
    assert (out["off"]["at"], out["off"]["filters"][0]) == ("/?all=library", "All40*")
    assert out["tab"] is False
    assert out["back"] == {"at": "/", "grid": None}
    assert (out["forward"]["at"], out["forward"]["filters"][0]) == ("/?all=library", "All40*")


def test_a_filter_that_cannot_be_had_leaves_the_one_that_stands_and_the_address() -> None:
    failed = _run("filters")["failed"]
    assert failed["at"] == "/?all=library&type=unseen&sort=az"
    assert failed["filters"][-1] == "Not watched3*"


def test_an_admin_has_filters_and_orders_of_their_own() -> None:
    out = _run("admins")
    assert (out["things"], out["sort"]) == (["Film 6"], "Largest")
    assert out["filters"][-2:] == ["Kept1*", "Stale0"]
    assert out["labels"][4:] == [
        "Admins only",
        "Largest",
        "Smallest",
        "Longest unwatched by anyone",
        "Last watched by anyone",
        "Leaving soonest",
    ]


def test_titles_in_hand_are_shown_without_asking_again() -> None:
    out = _run("handedOver")
    assert out["at"] == "/?all=library&type=movies&sort=size"
    assert (out["things"], out["calls"], out["back"]) == (["Film 6", "Film 5"], 0, "/")


def test_a_first_page_that_cannot_be_had_keeps_the_row_s_cards_and_offers_another_try() -> None:
    out = _run("firstPageFails")
    assert out["n"] == 30
    assert out["tail"] == "Couldn’t load more titles.Try again"


def test_the_way_out_of_an_empty_filter_hands_the_focus_to_the_filter_all() -> None:
    assert _run("emptyFilter") == {
        "says": "Show all titles",
        "at": "/?all=library",
        "things": ["Film 1", "Film 2", "Film 90"],
        "focus": "",
        "chip": True,
    }


def test_a_filter_or_order_chosen_while_the_grid_still_opens_is_taken() -> None:
    out = _run("whileItOpens")
    assert out["filter"] == {
        "moving": True,
        "at": "/?all=library&type=unseen",
        "still": False,
        "things": ["Film 2", "Film 8"],
    }
    assert (out["sort"]["at"], out["sort"]["things"]) == (
        "/?all=library&type=movies&sort=az",
        ["Film 9", "Film 1"],
    )
    assert out["leaving"]["at"] == "/?all=library&type=movies"


def test_a_filter_opened_by_its_address_is_brought_into_sight_at_once() -> None:
    assert _run("chosenInSight") == [[350, "auto"]]
