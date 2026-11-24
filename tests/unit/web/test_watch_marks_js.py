"""The Watched marks, the More menu and Dismiss on the Watch pages, run under node against a fake
DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_marks_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_menu_says_what_it_marks_and_offers_dismiss_only_in_continue_watching() -> None:
    assert _run("menus") == [
        ["Mark as watched", "Dismiss"],
        ["Mark episode as watched", "Dismiss"],
        ["Mark as watched", "Dismiss"],
        ["Mark as watched"],
        ["Mark show as not watched"],
        ["Mark season as watched"],
    ]


def test_a_film_marked_shows_it_on_all_its_cards_and_undo_puts_them_back() -> None:
    out = _run("markFilm")
    other = ["movie/9", False, "Movie", False]
    assert out["marked"] == [["movie/1", True, "Movie", False]] * 2 + [other]
    assert out["menuNow"] == ["Mark as not watched"]
    assert out["undone"] == [["movie/1", False, "Movie · 31 min left", True]] * 2 + [other]
    assert out["calls"] == [
        "POST /api/watch/watched/movie/1",
        "DELETE /api/watch/watched/movie/1?undo=1",
    ]
    assert out["toasts"][0] == "Marked Fieldwork as watched."


def test_a_mark_with_nothing_in_continue_watching_does_not_load_the_page_again() -> None:
    assert _run("markFilm")["fetched"] == []


def test_an_undo_the_server_refuses_leaves_the_mark_and_says_so() -> None:
    out = _run("undoRefused")
    assert out["look"] == [["movie/1", True, "Movie", False]]
    assert (out["variant"], out["busy"]) == ("error", None)


def test_a_mark_that_fails_changes_nothing_and_can_be_tried_again() -> None:
    out = _run("markFails")
    assert out["look"] == [["show/2", False, "TV · 2 seasons · 3 left", False]]
    assert "reach Plex" in out["toast"]
    assert out["free"] is None


def test_a_mark_that_changed_nothing_has_nothing_to_undo() -> None:
    assert _run("markFails")["noChange"] == {
        "look": [["movie/1", True, "Movie", False]],
        "undo": False,
    }


def test_one_change_at_a_time_and_the_toggle_says_when_it_is_in() -> None:
    assert _run("oneAtATime") == {
        "during": ["true", "false"],
        "calls": 1,
        "after": ["false", "true"],
    }


def test_marking_a_show_marks_its_own_season_cards_and_undo_takes_both_back() -> None:
    out = _run("markShow")
    assert [look[:3] for look in out["marked"]] == [
        ["show/2", True, "TV · 2 seasons"],
        ["season/21", True, "Season 1"],
        ["season/91", False, "Season 1"],
    ]
    assert [look[1:3] for look in out["undone"]] == [
        [False, "TV · 2 seasons · 3 left"],
        [False, "Season 1"],
        [False, "Season 1"],
    ]


def test_marking_a_season_updates_how_much_of_its_show_is_left() -> None:
    out = _run("markSeason")
    assert out["marked"]["look"][0][2] == "TV · 2 seasons · 4 left"
    # A Discover card's caption is the year.
    assert out["marked"]["look"][1][2] == "TV · 2016"
    assert out["marked"]["toast"] == "Marked Brightwater, Season 1 as watched."
    assert out["undone"][0][2] == "TV · 2 seasons · 9 left"


def test_a_dismissed_card_leaves_focus_goes_on_and_undo_brings_it_back_in_its_place() -> None:
    out = _run("dismiss")
    assert out["gone"] == {
        "marks": ["movie/1", "movie/3"],
        "focus": "movie/3",
        "calls": ["POST /api/watch/dismissed/show-2"],
        "toast": "Dismissed Brightwater from Continue watching.",
    }
    assert out["back"] == ["movie/1", "episode/22", "movie/3"]
    assert (out["focus"], out["call"]) == ("episode/22", "DELETE /api/watch/dismissed/show-2")


def test_a_dismissal_the_server_refuses_leaves_the_card() -> None:
    out = _run("dismissRefused")
    assert out == {"marks": ["movie/1", "episode/22", "movie/3"], "variant": "error"}


def test_dismissing_the_banner_shows_the_next_one_and_takes_the_title_s_card_too() -> None:
    out = _run("dismissBanner")
    assert out["gone"] == {
        "banner": ["Brightwater"],
        "marks": ["episode/22", "movie/3"],
        "focus": "watch-btn--play",
        "calls": ["POST /api/watch/dismissed/movie-1"],
    }
    assert out["back"] == {
        "banner": ["Fieldwork"],
        "marks": ["movie/1", "episode/22", "movie/3"],
    }


def test_dismissing_the_last_title_folds_the_banner_and_the_row_away_until_undo() -> None:
    out = _run("lastBanner")
    assert out["gone"] == {"banner": [], "marks": []}
    assert out["back"] == {"banner": ["Fieldwork"], "marks": ["movie/1"]}


def test_marking_in_continue_watching_goes_on_to_the_next_episode_or_takes_the_card_away() -> None:
    out = _run("markContinue")
    assert out["film"] == ["episode/22", "movie/3"]
    assert out["episode"] == {
        "marks": ["episode/23", "movie/3"],
        "focus": "episode/23",
        "toast": "Marked Brightwater, S1 · E2 as watched.",
    }
    assert out["undone"] == ["episode/22", "movie/3"]
    assert out["last"] == ["movie/1", "movie/3"]


def test_a_mark_undone_before_the_page_answers_moves_nothing() -> None:
    assert _run("undoneFirst") == ["movie/1", "episode/22", "movie/3"]


def test_under_details_the_rows_change_but_focus_stays_in_details() -> None:
    out = _run("underDetails")
    assert out["marks"] == ["episode/22", "movie/3"]
    assert (out["focus"], out["afterUndo"]) == ("in-details", "in-details")
    assert out["touched"] == "BODY"
    assert out["events"][0] == "movie-1"


def test_a_show_page_follows_an_episode_s_mark_where_it_stands() -> None:
    out = _run("showPage")["same"]
    assert out["fetched"] == ["/s?season=1"]
    assert (out["play"], out["show"], out["focus"]) == ("/watch/play/22", "true", True)
    # The tab's words stay its own: the mark is in its name.
    assert out["pill"] == ["Season 1, watched", "Season 1", True]
    assert out["episode"] == ["true", "Mark as not watched", "One, watched", False, False]


def test_a_season_picked_meanwhile_is_left_alone() -> None:
    assert _run("showPage")["otherSeason"] == ["/watch/play/21", "false"]
