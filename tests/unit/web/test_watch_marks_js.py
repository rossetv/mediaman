"""The Watched marks, the More menu and Dismiss on the Watch pages, run under node against a fake
DOM."""

from __future__ import annotations

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import jinja2
import pytest

TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"
HARNESS = Path(__file__).parent / "js" / "watch_marks_harness.js"


def _run(scenario: str, *args: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario, *args], capture_output=True, text=True, check=False
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


# What a caption reads, and how its words are laid out: (class, words), "said" is read out
# and not drawn, "wide" and "mid" are drawn only past phones, and a unit drawn there in their place.
CAPTIONS = {
    "Movie": [["", "Movie"]],
    "Movie · 2026": [["lead", "Movie"], ["said", " · "], ["tail", "2026"]],
    "Movie · 1 h 30 min left": [
        ["lead", "Movie"],
        ["said", " · "],
        ["tail", "1"],
        ["wide", " h"],
        ["unit", "h"],
        ["tail", " 30"],
        ["wide", " min"],
        ["unit", "m"],
        ["tail", " left"],
    ],
    "Movie · 2026 · 31 min left": [
        ["lead", "Movie"],
        ["said", " · "],
        ["mid", "2026 · "],
        ["end", "31"],
        ["wide", " min"],
        ["unit", "m"],
        ["end", " left"],
    ],
    "Anime · 1 season": [["lead", "Anime"], ["said", " · "], ["tail", "1 season"]],
    "TV · 10 seasons · 3 left": [
        ["lead", "TV"],
        ["said", " · "],
        ["mid", "10 seasons · "],
        ["end", "3 left"],
    ],
    "S2 · E4 · 12 min left": [
        ["lead", "S2"],
        ["said", " ·"],
        ["lead", " E4"],
        ["said", " · "],
        ["tail", "12"],
        ["wide", " min"],
        ["unit", "m"],
        ["left", " left"],
    ],
    "E4 · 12 min left": [
        ["lead", "E4"],
        ["said", " · "],
        ["tail", "12"],
        ["wide", " min"],
        ["unit", "m"],
        ["left", " left"],
    ],
    "S2 · E4": [["lead", "S2"], ["said", " ·"], ["lead", " E4"]],
    "Season 2": [["", "Season 2"]],
    "": [],
}


def _said(pieces: list[list[str]]) -> str:
    return "".join(words for kind, words in pieces if kind != "unit")


class _Pieces(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.open: list[str] = []
        self.pieces: list[list[str]] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        self.open.append(attr["class"].removeprefix("watch-card__"))
        if attr.get("data-unit"):
            assert attr["aria-hidden"] == "true"
            self.pieces.append(["unit", attr["data-unit"]])

    def handle_endtag(self, tag):
        self.open.pop()

    def handle_data(self, data):
        if self.open or data:
            self.pieces.append([self.open[-1] if self.open else "", data])


def test_a_caption_says_all_of_it_and_marks_what_a_narrow_card_leaves_out() -> None:
    out = _run("captions", json.dumps(list(CAPTIONS)))
    assert dict(out) == CAPTIONS
    assert [_said(pieces) for _, pieces in out] == list(CAPTIONS)


def test_a_card_a_script_builds_has_the_same_caption() -> None:
    out = _run("builtCaption")
    assert out["library"][0] == "Movie · 31 min left"
    assert out["library"][1] == ["lead", "Movie"]
    # A title in the library that a TMDB search finds has More beside its caption too.
    assert out["found"] == ["TV · 2016", ["lead", "TV"], ["said", " · "], ["tail", "2016"]]


@pytest.mark.parametrize("text", CAPTIONS)
def test_the_page_draws_the_captions_the_script_draws(text: str) -> None:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    html = str(env.get_template("_watch_card.html").module.caption(text))
    assert _Pieces(html).pieces == CAPTIONS[text]


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


def test_a_show_card_past_the_page_s_cards_is_brought_up_to_date_by_its_details_and_search() -> (
    None
):
    out = _run("markSeasonPastThePage")
    assert out["found"]["marked"][0] == ["show/2", False, "TV · 2 seasons · 4 left", False]
    assert out["found"]["marked"][1][2] == "TV · 2016"
    assert out["found"]["undone"][0][2] == "TV · 2 seasons · 9 left"
    assert out["finished"]["marked"][0][1:3] == [True, "TV · 2 seasons"]
    # Whether it is watched does not hang on the search finding it.
    assert out["unlisted"]["marked"][0][1:3] == [True, "TV · 2 seasons"]
    assert out["noLonger"]["marked"][0][1:3] == [False, "TV · 2 seasons"]
    # Not known any more: nothing is said rather than something wrong.
    assert out["failed"]["marked"][0][1:3] == [False, "TV · 2 seasons"]


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


def test_a_dismissal_in_a_row_opened_as_a_grid_leaves_both_and_comes_back_to_both() -> None:
    out = _run("dismissInGrid")
    assert out["gone"] == [["movie/1", "movie/3"]] * 2
    assert out["back"] == [["movie/1", "episode/22", "movie/3"]] * 2
    assert out["calls"] == [
        "POST /api/watch/dismissed/show-2",
        "DELETE /api/watch/dismissed/show-2",
    ]


def test_an_undo_gives_the_focus_to_a_control_that_is_shown() -> None:
    assert _run("undoFocusInGrid") == {
        "dismissed": "grid continue",
        "fromPreview": "grid continue",
        "watched": "grid continue",
        "libraryCard": "grid library",
        "closed": "row continue",
        "toggledElsewhere": "grid library",
        # The rows on their way out are laid out and inert.
        "opening": "grid continue",
        "libraryClosed": "row continue",
    }


def test_a_card_fading_out_of_the_grid_is_taken_out_once() -> None:
    assert _run("ghostInGrid") == [["episode/22", "movie/3"], ["movie/1"], False]


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


def test_a_banner_dismissed_by_touch_hands_the_focus_to_the_next_banner_alone() -> None:
    assert _run("dismissBannerUnfocused") == ["watch-btn--play"]


def test_a_card_an_undo_puts_back_out_of_sight_is_scrolled_to_in_its_row() -> None:
    assert _run("undoOffScreen") == {
        "before": ["movie/1", 0, [[-128, "auto"]], ["none"], ""],
        "after": ["movie/1", 0, [[10, "auto"]], ["none"], ""],
        "inView": ["movie/1", 0, [], ["none"], ""],
        "mark": ["movie/1", 0, [[-128, "auto"]], ["none"], ""],
    }


def test_a_card_an_older_undo_brings_from_the_server_is_scrolled_to_too() -> None:
    assert _run("olderUndoOffScreen") == {
        "brought": ["movie/3"],
        "marks": ["movie/1", "movie/3"],
        "by": [[-128, "auto"]],
        "snap": "",
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
    # Details is told once, and Ctrl+Z reaches the Undo of a mark made in it.
    assert out["events"] == ["movie-1"]
    assert (out["dialog"], out["onPage"]) == ("ttl", None)


def test_a_show_page_follows_an_episode_s_mark_where_it_stands() -> None:
    out = _run("showPage")["same"]
    assert out["fetched"] == ["/s?season=1"]
    assert (out["play"], out["show"], out["focus"]) == ("/watch/play/22", "true", True)
    # The tab's words stay its own: the mark is in its name.
    assert out["pill"] == ["Season 1, watched", "Season 1", True]
    assert out["episode"] == ["true", "Mark as not watched", "One, watched", False, False]


def test_a_season_picked_meanwhile_is_left_alone() -> None:
    assert _run("showPage")["otherSeason"] == ["/watch/play/21", "false"]


def test_a_late_answer_about_the_show_s_page_never_undoes_a_newer_mark() -> None:
    assert _run("lateTitlePage") == {
        "pressed": ["true", "true", "false"],
        "play": "/watch/play/23",
    }


def test_a_late_answer_about_the_show_s_page_never_brings_back_an_undone_mark() -> None:
    assert _run("lateTitlePageUndone") == {
        "pressed": ["false", "false", "false"],
        "play": "/watch/play/21",
    }


def test_rows_whose_answer_came_too_late_follow_with_the_next_change() -> None:
    assert _run("lateRows") == ["movie/1", "episode/23", "movie/3"]


def test_a_card_s_open_menu_closes_when_the_card_goes_and_another_s_stays() -> None:
    out = _run("menuOfAGoneCard")
    assert out["replaced"] == [False, "episode/23"]
    assert out["undone"] is False
    assert out["another"] == [True, True]
    assert out["left"] == [False, "movie/1"]


def test_undoing_an_older_mark_shows_the_server_s_card_once_and_in_its_place() -> None:
    out = _run("undoOldestMark")
    row = ["movie/1", "episode/22", "movie/3"]
    assert out["replaced"] == {
        "marked": ["movie/1", "episode/24", "movie/3"],
        "oldest": row,
        "both": row,
        "rows": 1,
        "started": 0,
    }
    assert out["left"] == {
        "marked": ["movie/1", "movie/3"],
        "oldest": row,
        "both": row,
        "rows": 1,
        "started": 0,
    }
    # The row came back with the card, with its arrows.
    assert out["rowGone"] == {
        "marked": [],
        "oldest": ["episode/22"],
        "both": ["episode/22"],
        "rows": 1,
        "started": 1,
    }


def test_an_undone_title_joins_the_row_the_server_s_page_brought_back() -> None:
    assert _run("undoIntoANewRow") == {
        "one": ["episode/22"],
        "both": ["episode/22", "movie/3"],
        "rows": 1,
    }


def test_banners_undone_oldest_first_show_the_server_s_banner_each_time() -> None:
    out = _run("undoOldestBanner")
    assert out["states"] == [[], ["Fieldwork"], ["Fieldwork"], ["Fieldwork"]]
    assert out["marks"] == ["movie/1", "episode/22", "movie/3"]


def test_undoing_an_older_season_s_mark_says_what_is_left_now() -> None:
    assert _run("undoOldestSeason") == [
        [False, "TV · 2 seasons · 5 left"],
        [False, "Season 21"],
        [True, "Season 22"],
    ]


def test_an_undone_dismissal_stands_while_the_server_cannot_ask_plex() -> None:
    out = _run("undoWithoutPlex")
    assert out["gone"] == [["Fieldwork"], ["movie/1", "movie/3"]]
    assert out["back"] == [["Brightwater"], ["movie/1", "episode/22", "movie/3"]]


def test_an_undo_gives_the_focus_to_what_came_back_when_it_took_the_focused_button_away() -> None:
    assert _run("undoFocus") == {
        "replaced": "episode/22",
        "inBanner": ["Fieldwork"],
        "redrawn": "episode/22",
        "elsewhere": "watch-search",
    }


def test_a_late_answer_about_a_show_never_brings_back_an_undone_mark() -> None:
    assert _run("lateShow") is False


def test_an_open_grid_is_filled_for_a_card_the_server_s_page_brings() -> None:
    told = [[True, "movie-3", True, "cont"]]
    assert _run("entersTheGrid") == {"newRow": told, "into": told}


def test_an_undo_waits_for_the_same_title_s_change_on_its_way() -> None:
    out = _run("undoWaits")
    assert out["waiting"] == 2
    assert out["calls"] == [
        "POST /api/watch/watched/movie/1",
        "DELETE /api/watch/watched/movie/1",
        "DELETE /api/watch/watched/movie/1?undo=1",
    ]


def test_a_second_dismiss_before_the_first_is_in_does_nothing() -> None:
    assert _run("dismissTwice") == [["POST /api/watch/dismissed/movie-1"], 1]


def test_a_show_page_follows_only_what_nothing_else_is_changing() -> None:
    out = _run("leftAlone")
    before, after = "/watch/play/21", "/watch/play/22"
    assert out == {
        "busyMark": [after, ["true", "true", "false"]],
        "loadingSeason": [before, ["true", "true", "false"]],
        "signedOut": [before, ["true", "true", "false"]],
        "playFocused": [before, ["true", "false", "false"]],
        "followed": [after, ["true", "false", "false"]],
    }


def test_a_title_marked_as_not_watched_again_comes_back_to_continue_watching() -> None:
    out = _run("notWatchedAgain")
    row = ["movie/1", "episode/22", "movie/3"]
    assert out["film"] == {"marked": ["episode/22", "movie/3"], "again": row}
    assert out["show"] == {"marked": ["movie/1", "movie/3"], "again": row}


def test_dismissing_a_show_meanwhile_loses_nothing_of_its_episode_s_mark() -> None:
    assert _run("dismissedMeanwhile") == {
        "page": ["TV · 8 left", "TV · 8 left"],
        "show": ["TV · 8 left", "TV · 8 left"],
    }


def test_marking_the_whole_show_meanwhile_still_says_what_is_left_of_it() -> None:
    assert _run("wholeShowMeanwhile") == [False, "TV"]


def test_a_rating_in_the_menu_is_saved_and_shuts_it_and_one_taken_back_does_not() -> None:
    out = _run("menuRating")
    rated = "PUT /api/watch/ratings/movie-1 1"
    assert out["first"] == [False, [rated], "movie/1"]
    assert out["back"] == [True, ["DELETE /api/watch/ratings/movie-1"]]


def test_the_menus_keys_reach_the_four_thumbs_and_the_items() -> None:
    assert _run("menuKeys") == [
        "Strong no",
        "Not for me",
        "I like it",
        "Mark as watched",
        "Strong no",
        "I love it",
        "Mark as watched",
    ]


def test_a_strong_no_takes_the_title_out_of_the_picks_alone_and_undo_gives_the_old_level_back() -> (
    None
):
    out = _run("strongNo")
    assert out["gone"] == ["movie/8", "movie/9", "movie/1"]
    assert out["toast"] == "Fieldwork is gone from your picks. Search still finds it."
    assert out["back"] == ["movie/8", "movie/1", "movie/9"]
    assert out["calls"] == [f"PUT /api/watch/ratings/movie-1 {level}" for level in (1, -2, 1)]


def test_the_focus_goes_to_the_next_pick_and_back_with_undo() -> None:
    assert _run("strongNoFocus") == {"next": "movie/9", "back": "movie/1"}


def test_a_pick_rated_in_details_goes_behind_it_and_closing_lands_on_the_next() -> None:
    assert _run("strongNoInDetails") == {
        "open": [["movie/8"], "inside", "ttl"],
        "closed": "movie/8",
    }


def test_a_strong_no_that_was_not_saved_takes_nothing_out_and_is_said() -> None:
    assert _run("ratingFails") == {
        "picks": ["movie/8", "movie/1", "movie/9"],
        "toasts": ["Your rating wasn’t saved. Try again."],
    }


def test_an_undo_that_is_refused_takes_the_pick_out_again() -> None:
    out = _run("undoFails")
    assert out["now"] == ["movie/8", "movie/1", "movie/9"]
    assert out["after"] == ["movie/8", "movie/9"]
    assert out["toast"] == "Couldn’t bring Fieldwork back. It stays out of your picks."


def test_a_title_rated_otherwise_later_is_back_among_the_picks_and_its_toast_goes() -> None:
    assert _run("secondThoughts") == {
        "gone": ["movie/8", "movie/9"],
        "back": ["movie/8", "movie/1", "movie/9"],
        "dismissed": 1,
    }


def test_an_undo_leaves_a_row_filled_anew_alone_and_brings_an_emptied_one_back() -> None:
    assert _run("undoLater") == {"anew": ["movie/8"], "emptied": False, "back": ["movie/1"]}


def test_a_change_takes_the_toast_of_the_same_titles_last_one_away() -> None:
    marked = "Marked Fieldwork as watched."
    salt = "Marked Salt as watched."
    assert _run("markToasts") == [[], [], [marked], [marked, salt]]


def test_dismissing_again_takes_the_toasts_of_the_last_dismiss_and_its_undo_away() -> None:
    assert _run("dismissToasts") == [
        "Dismissed Fieldwork from Continue watching.",
        "Fieldwork is back in Continue watching.",
    ]
