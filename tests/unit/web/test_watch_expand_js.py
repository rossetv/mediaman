"""The rows of the Watch page that grow into grids, run under node against a fake DOM and history."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_expand_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_row_s_heading_opens_its_grid_which_goes_on_from_the_row_and_back_returns() -> None:
    out = _run("library")
    assert out["label"] == "Movies, see all 32"
    assert (out["prevented"], out["at"]) == (True, "/watch?all=movies")
    assert (out["title"], out["count"], out["sort"]) == ("Movies", "32 titles", "Recently added")
    assert (out["n"], out["last"]) == (32, ["Film 31", "Film 32"])
    assert out["calls"] == ["/api/watch/library/movies/row?after=c30&limit=60"]
    assert out["rows"] is True  # hidden behind it
    assert out["back"] == {"at": "/watch", "grid": None, "rows": True, "focus": True}


def test_the_order_chosen_comes_from_the_server_and_stays_in_the_address() -> None:
    out = _run("sorted")
    assert out["labels"] == ["Recently added", "A to Z"]
    assert out["sorted"]["at"] == "/watch?all=movies&sort=az"
    assert out["sorted"]["grid"]["things"] == ["Film 9", "Film 1"]
    assert out["back"] == {"at": "/watch", "grid": None}
    assert out["forward"] == {
        "at": "/watch?all=movies&sort=az",
        "things": ["Film 9", "Film 1"],
        "sort": "A to Z",
    }
    assert out["again"] == {"at": "/watch?all=movies", "n": 2, "first": "Film 1"}


def test_a_row_all_on_the_page_is_put_in_order_there_as_the_server_would() -> None:
    out = _run("continued")
    assert out["first"]["things"][0] == "The Zebra"
    assert out["sorted"] == ["An Apple", "Éclair", "Fig", "Gnu", "Orchard", "The Zebra"]
    assert (out["at"], out["calls"]) == ("/watch?all=continue&sort=az", [])


def test_trending_shows_films_or_tv_alone_on_every_page_and_counts_nothing() -> None:
    out = _run("filtered")
    assert out["all"] == ["T1", "T2", "T3", "T4", "T5"]
    assert (out["count"], out["tail"]) == ("", "That’s everything for now.")
    assert (out["tv"], out["at"]) == (["T2", "T4"], "/watch?all=trending&sort=tv")


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


def test_a_row_that_is_all_on_the_screen_offers_no_grid() -> None:
    assert _run("whole") == {
        "plain": {"href": None, "label": None, "cue": False},
        "prevented": False,
        "at": "/watch",
        "grid": None,
    }


def test_three_screens_of_one_row_bring_the_offer_and_another_row_takes_it_away() -> None:
    out = _run("offered")
    assert out["seen"] == [False, False, True]
    assert (out["text"], out["endless"]) == ("See all 32 in a grid", "Keep going in a grid")
    assert (out["taken"], out["again"], out["back"]) == (False, False, True)
    assert (out["at"], out["title"], out["left"]) == ("/watch?all=movies", "Movies", False)


def test_a_click_for_a_new_tab_or_on_an_unknown_row_is_the_browser_s() -> None:
    assert _run("otherClicks") == {"tab": False, "unknown": False, "grid": None, "at": "/watch"}


def test_a_shared_link_keeps_the_server_s_cards_and_goes_on_after_them() -> None:
    out = _run("cold")
    assert out["open"]["things"] == ["Film 1", "Film 2", "Film 3", "Film 4", "Film 5"]
    assert out["same"] is True
    # Back from a link leads to the rows in the same entry.
    assert (out["at"], out["rows"], out["entries"]) == ("/watch", True, 1)


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
    assert out["back"] == {"at": "/watch?all=movies", "title": "Movies"}
    assert out["browse"] == {"at": "/watch", "grid": None, "rows": True}


def test_an_address_that_asks_for_something_else_or_for_nothing_known_opens_no_grid() -> None:
    assert _run("odd") == {
        "search": None,
        "list": None,
        "viewer": ["Movies", "Recently added"],
        "genre": None,
        "unknown": None,
        "inherited": None,
        "order": ["Continue watching", "Last watched"],
    }


def test_a_row_s_grid_keeps_nothing_of_a_category_in_its_address() -> None:
    assert _run("afterACategory") == "/watch?all=movies"


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
        "href": "/watch?all=continue",
        "label": "Continue watching, see all 5",
        "cue": True,
        "offer": "See all 5 in a grid",
    }
