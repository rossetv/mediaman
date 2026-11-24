"""The preview a Watch card grows into under the mouse, run under node against a fake DOM and a
fake clock."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_preview_harness.js"
CSS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch_preview.css"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_passing_over_many_cards_asks_nothing_and_opens_nothing() -> None:
    assert _run("passing") == {"gets": [], "open": None}


def test_resting_on_a_card_opens_its_preview_out_of_the_tab_order_and_of_screen_readers() -> None:
    assert _run("opens") == {
        "gets": ["movie-1"],
        "title": "Fieldwork",
        "meta": ["20241 h 56 min15IMDb 7.4RT 88%", "Drama"],
        "text": "A field.",
        "play": "/watch/play/1",
        "hiddenFromReaders": "true",
        "stops": 0,
    }


def test_a_card_left_before_its_reply_drops_the_request_and_the_late_answer() -> None:
    assert _run("leftBeforeReply") == {
        "asked": ["movie-1"],
        "aborted": ["movie-1"],
        "open": None,
        "askedAgain": 2,
    }


def test_a_slow_reply_opens_on_skeletons_and_fills_the_same_preview() -> None:
    out = _run("slow")
    assert out["early"] == {"title": "Fieldwork", "text": None, "skels": True}
    assert (out["same"], out["text"], out["skels"]) == (True, "A field.", 0)


def test_a_failure_leaves_the_card_alone_and_is_not_asked_again_for_a_minute() -> None:
    assert _run("failed") == {"open": None, "soon": 1, "later": 2}


def test_a_preview_open_on_skeletons_folds_away_when_its_reply_fails() -> None:
    assert _run("slowThenFailed") == {"before": "Fieldwork", "after": None, "layers": 0}


def test_a_server_that_asks_for_a_pause_gets_one_for_every_title() -> None:
    assert _run("refused") == {"soon": ["movie-1"], "later": ["movie-1", "movie-2"]}


def test_a_title_is_asked_once_until_it_changes_or_goes_stale() -> None:
    assert _run("cached") == {"first": 1, "second": 1, "marked": 2, "changed": 3, "stale": 4}


def test_a_second_card_drops_the_request_of_the_first_and_only_it_is_open() -> None:
    assert _run("oneAtATime") == {
        "gets": ["movie-1", "movie-2"],
        "aborted": ["movie-1"],
        "title": "Orbit",
        "layers": 1,
    }


def test_a_press_on_the_card_is_never_overtaken_by_a_preview() -> None:
    assert _run("pressed") == {"open": None, "aborted": ["movie-1"]}


def test_a_touch_or_a_touch_screen_never_opens_one() -> None:
    assert _run("touch") == [[], []]


def test_a_scroll_closes_it_and_another_opens_only_once_the_page_has_settled() -> None:
    assert _run("scrolled") == {"open": None, "tooSoon": None, "later": "Orbit"}


def test_a_card_that_moves_under_a_still_mouse_opens_nothing() -> None:
    assert _run("stillMouse") == []


def test_escape_and_tab_close_it_and_other_keys_do_not() -> None:
    assert _run("keys") == {"Escape": None, "Tab": None, "a": "Fieldwork"}


def test_nothing_opens_under_an_open_menu_the_detail_or_a_grid_on_the_move() -> None:
    assert _run("covered") == [[], []]


def test_a_hidden_tab_drops_the_preview_and_its_trailer_at_once() -> None:
    assert _run("hidden") == {"before": True, "layers": 0, "frame": False}


def test_a_title_the_library_holds_is_played_from_the_library() -> None:
    assert _run("follows") == {"gets": ["tmdb-movie-9"], "play": "/watch/play/1"}


def test_addresses_and_keys_from_a_reply_are_never_taken_on_trust() -> None:
    assert _run("hostile") == {
        "first": False,
        "trailer": False,
        "pictures": ["/api/poster/1?kind=art"],
        "then": [],
        "posts": [],
    }


def test_the_trailer_is_youtubes_private_player_muted_and_only_after_a_look() -> None:
    out = _run("trailerWaits")
    assert (out["early"], out["shown"]) == (False, False)
    assert out["src"].startswith(
        "https://www.youtube-nocookie.com/embed/AbCdEfGhIjK?autoplay=1&mute=1"
    )
    assert "allow-top-navigation" not in out["sandbox"]


def test_the_trailer_shows_only_on_its_own_players_word_and_its_sound_by_a_press() -> None:
    out = _run("trailerPlays")
    assert (out["forged"], out["playing"], out["stillThere"], out["ended"]) == (
        False,
        True,
        True,
        True,
    )
    assert (out["before"], out["on"]) == ("Turn sound on", "Turn sound off")
    assert ["unMute", "https://www.youtube-nocookie.com"] in out["said"]


def test_a_trailer_left_is_muted_at_once_and_removed_with_the_preview() -> None:
    out = _run("trailerLeft")
    assert (out["said"][-1], out["frame"]) == ("mute", False)


def test_a_trailer_that_cannot_play_leaves_the_preview_and_is_not_tried_again() -> None:
    assert _run("trailerBad") == {
        "error": [True, False],
        "blocked": [True, False],
        "stuck": [True, False],
    }


def test_no_trailer_plays_by_itself_where_the_person_would_not_want_it() -> None:
    assert _run("trailerNot") == {
        "plain": True,
        "reduced": False,
        "saving": False,
        "off": False,
        "none": False,
    }


def test_the_switch_stops_the_trailer_playing_and_the_ones_to_come() -> None:
    out = _run("switchOff")
    assert out == {"frame": False, "kept": "off", "next": False, "title": "Orbit"}


def test_download_is_sent_once_and_every_card_is_told() -> None:
    out = _run("download")
    assert out["posts"] == [
        ["/api/search/download", {"media_type": "movie", "tmdb_id": 9, "title": "Salt"}]
    ]
    assert out["said"] == [["tmdb-movie-9", "queued"]]
    assert out["now"] == "Queued"


def test_a_download_that_fails_can_be_tried_again_and_one_already_there_cannot() -> None:
    assert _run("downloadFails") == {
        "500": ["Download", True, 1],
        "409": ["Queued", False, 1],
    }


def test_one_season_downloads_from_here_and_a_choice_of_seasons_opens_the_detail() -> None:
    out = _run("downloadShow")
    assert out["one"][0][1]["search_seasons"] == [2]
    assert out["one"][0][1]["monitored_seasons"] == [2, 1]
    assert (out["label"], out["many"], out["opened"]) == ("Pick seasons", [], ["tmdb-tv-7"])


def test_a_continue_watching_card_resumes_at_once_and_its_menu_works_the_episode() -> None:
    out = _run("going")
    assert out["gets"] == ["show-2"]
    assert (out["label"], out["href"]) == ("Resume", "/watch/play/22?resume=1")
    assert out["where"] == "S2 · E4, 23 min left · Drama"
    assert out["menu"] == ["Mark episode as watched", "Dismiss", "Play trailers on hover"]
    assert out["pinned"] == "The Long Coast"
    assert out["asked"] == [
        ["data-mark-toggle", "episode/22"],
        ["data-mark-name", "The Long Coast, S2 · E4"],
        ["aria-pressed", "false"],
        ["data-watch-dismiss", "show-2"],
        ["data-mark-name", "The Long Coast"],
    ]
    assert out["closed"] is None


def test_details_opens_the_title_of_a_card_that_plays() -> None:
    assert _run("details") == {"opened": ["show-2"], "open": None}


def test_a_press_on_the_picture_or_words_is_the_cards_and_a_modified_one_the_links() -> None:
    assert _run("picture") == 2


def test_without_a_wide_picture_the_poster_stands_in() -> None:
    assert _run("art") == {
        "srcs": ["/api/poster/1?kind=art", "/img/b.jpg"],
        "standIn": True,
    }


def test_a_picks_reason_has_its_place_before_it_arrives() -> None:
    assert _run("pick") == {"held": True, "reason": "AI pickYou finished Fieldwork."}


def test_no_button_can_be_pressed_while_the_preview_grows_under_the_mouse() -> None:
    css = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.pv:not\(\.is-settled\) \.pv__body \{([^}]*)\}", css)
    assert rule is not None
    assert "pointer-events: none" in rule[1]


def test_the_page_closes_it_and_calls_off_the_next_when_it_moves_on() -> None:
    assert _run("movedOn") == {
        "before": "Fieldwork",
        "after": [None, False, True],
        "layers": 0,
        "gets": ["movie-1"],
    }


def test_a_pick_the_library_holds_keeps_its_reason_here_and_in_its_detail() -> None:
    assert _run("heldPick") == {
        "gets": ["rec-6"],
        "reason": "AI pickYou finished Fieldwork.",
        "opened": ["movie-3"],
        "pick": "rec-6",
        "kind": "movie",
    }


def test_escape_closes_the_preview_alone_before_the_grid_or_the_search_under_it() -> None:
    out = _run("escapeInGrid")
    assert out["page"] == ["Fieldwork", [None, True, 0, 0], [None, True, 1, 0]]
    assert out["search"] == ["Fieldwork", [None, True, 0, 0], [None, True, 0, 1]]


def test_escape_closes_the_previews_menu_then_the_preview_then_goes_back() -> None:
    assert _run("escapeInGrid")["menu"] == [
        "Fieldwork",
        ["Fieldwork", True, 0, 0],
        [None, True, 0, 0],
        [None, True, 1, 0],
    ]


def test_an_escape_that_ends_a_composition_leaves_the_preview() -> None:
    assert _run("escapeComposing") == ["Fieldwork", False]


def _rule(selector: str) -> str:
    rule = re.search(r"^" + re.escape(selector) + r" \{([^}]*)\}", CSS.read_text("utf-8"), re.M)
    assert rule is not None
    return rule[1]


def test_a_preview_past_the_windows_edge_gives_the_page_no_sideways_scroll() -> None:
    assert _run("layer") == ["pv-layer", True]
    assert "position: absolute" in _rule(".pv-layer")
    assert "overflow-x: clip" in _rule(".pv-layer")


def test_the_trailers_frame_takes_no_press_so_the_focus_and_the_mouse_stay_the_pages() -> None:
    assert "pointer-events: none" in _rule(".pv__frame")


def test_focus_left_in_a_preview_or_its_menu_goes_to_the_card_as_it_closes() -> None:
    assert _run("focusBack") == {"escape": True, "scroll": True, "hidden": True, "menuOpen": True}


def test_a_preview_that_closes_leaves_a_focus_that_was_elsewhere() -> None:
    assert _run("focusElsewhere") == "watch-search"


def test_a_title_kept_from_its_preview_leaves_the_focus_for_keep_to_place() -> None:
    assert _run("keptFromMenu") == [None, True]


def test_a_preview_whose_card_goes_fades_where_it_stands_with_its_trailer() -> None:
    out = _run("cardGone")
    assert out["removed"] == ["Fieldwork", None, True, True, False, 0]
    assert out["hidden"] == ["Fieldwork", None, True, True, False, 0]
    assert out["stays"] == ["Fieldwork", "Fieldwork", False, True, True, 1]


def test_a_preview_is_never_folded_into_a_card_that_has_gone() -> None:
    assert _run("escapeAfterGone") == [True, []]


def test_back_resize_typing_a_drag_and_a_mouse_out_of_the_window_close_it_or_call_it_off() -> None:
    assert _run("waysOut") == {
        "back": ["Fieldwork", "fades", False, 0, False, 0],
        "resize": ["Fieldwork", "fades", False, 0, False, 0],
        "typing": ["Fieldwork", "fades", False, 0, False, 0],
        "mouseOut": ["Fieldwork", "folds", False, 0, False, 0],
        "drag": ["Fieldwork", "folds", False, 0, False, 0],
        "pagehide": ["Fieldwork", "gone", False, 0, False, 0],
    }


def test_a_mouse_out_of_the_window_leaves_a_preview_whose_menu_is_open() -> None:
    assert _run("mouseOutPinned") == "Fieldwork"


def test_a_press_on_its_card_hands_it_to_the_detail_and_on_another_closes_it() -> None:
    assert _run("cardPressed") == {"film": [True, None, 0], "other": [False, None, 0]}


def test_back_on_the_card_mid_fold_the_same_preview_turns_round() -> None:
    assert _run("reopened") == {
        "folding": [None, False],
        "back": [True, True],
        "later": True,
        "layers": 1,
        "gets": 1,
    }


def test_a_card_crossed_fast_waits_until_the_mouse_rests() -> None:
    assert _run("crossingFast") == {"fast": 0, "atRest": "Fieldwork"}


def test_a_card_more_than_half_out_of_sight_or_with_no_room_opens_nothing() -> None:
    assert _run("noRoom") == {
        "whole": "Fieldwork",
        "justOver": "Fieldwork",
        "mostlyLeft": None,
        "mostlyRight": None,
        "mostlyUnder": None,
        "tooTall": [None, 0],
    }


def test_a_dialog_that_opens_while_the_mouse_rests_calls_it_off() -> None:
    assert _run("dialogWhileArmed") == []


def test_a_press_in_a_preview_takes_no_focus() -> None:
    assert _run("focusKept") is True


def test_a_trailer_leaves_no_timer_running_however_it_ends() -> None:
    out = _run("trailerTimers")
    assert out.pop("beforeItStarts") == [0, False]
    for running, left in out.values():
        assert running > 0
        assert left == 0


def test_a_card_a_view_puts_under_a_mouse_at_rest_opens_nothing_until_it_moves() -> None:
    opened = [None, "Fieldwork"]
    assert _run("viewChanged") == {
        "fresh": opened,
        "detail": opened,
        "back": opened,
        "moved": opened,
    }


def test_keep_acts_on_the_season_its_card_shows_and_names_it() -> None:
    out = _run("seasonsLeaving")
    season_2 = [
        "202",
        "Keep The Long Coast, Season 2",
        "Keep Season 2",
        "Season 2 leaves in 9 days",
    ]
    season_3 = [
        "203",
        "Keep The Long Coast, Season 3",
        "Keep Season 3",
        "Season 3 leaves in 3 days",
    ]
    assert out["season2"] == season_2
    assert out["season3"] == season_3
    # An episode's card shows its season; a whole show's, what leaves first.
    assert out["going"] == season_2
    assert out["wholeShow"] == season_3
    assert out["gets"] == ["show-2"]


def test_a_card_of_a_season_that_is_not_leaving_offers_no_keep_of_another() -> None:
    out = _run("seasonsLeaving")
    assert out["onAnother"] == [None, None, None, None]
    assert out["keptSeason"] == [None, None, None, None]


def test_a_season_on_a_card_as_leaving_can_be_kept_whatever_an_older_reply_says() -> None:
    assert _run("leavingSince") == "202"


def test_keeping_one_season_from_its_preview_keeps_that_one_and_says_which() -> None:
    assert _run("keptSeasonToast") == {
        "posts": [["/api/watch/keep", {"media_item_id": "202", "days": 30}]],
        "toasts": ["Kept The Long Coast, Season 2 for 30 days."],
        "cards": [False, True],
        "asked": 2,
    }


def test_a_keep_or_undo_made_anywhere_is_in_the_next_preview_and_the_open_one() -> None:
    title = "The Long Coast"
    first = [title, "Season 3 leaves in 3 days", "203"]
    second = [title, "Season 2 leaves in 9 days", "202"]
    nothing = [title, None, None]
    assert _run("keptElsewhere") == {
        "kept": second,
        "undone": first,
        "openAtOnce": nothing,
        "openThen": [*second, 1],
        "onItsWay": second,
        "other": ["Fieldwork", 2, "true"],
        "admin": nothing,
    }


def test_a_preview_stays_under_the_header_of_an_open_grid() -> None:
    out = _run("underGridHead")
    assert out["clear"] == "translate(275px,124px)"
    assert out["justOver"] == "translate(275px,124px)"
    assert out["mostlyUnder"] is None


def test_what_the_keyboard_opens_over_the_cards_closes_a_preview_and_its_trailer() -> None:
    assert _run("keyboardCovers") == {
        "details": [None, False],
        "dialog": [None, False],
        "menu": [None, False],
        "ownMenu": ["Fieldwork", True],
        "nothing": ["Fieldwork", True],
    }


def test_a_card_pressed_still_hands_its_preview_over_when_the_page_is_told_first() -> None:
    assert _run("pressedThenTold") is True


def test_what_the_storage_panel_schedules_or_clears_is_in_the_next_preview_and_the_open_one() -> (
    None
):
    leaving = ["Season 3 leaves in 3 days", True]
    assert _run("scheduled") == {"before": leaving, "cleared": [None, False], "open": leaving}


def test_a_mark_is_in_the_open_preview_and_in_a_reply_on_its_way() -> None:
    out = _run("marked")
    going = ["Resume", "/watch/play/1?resume=1", "1 h 23 min left · Drama", 1]
    seen = ["Play", "/watch/play/1", "Drama", 0]
    assert (out["before"], out["another"]) == (going, 1)
    assert out["marked"] == [*seen, True, 1]
    assert out["undone"] == going
    assert out["failed"] is None
    assert out["onItsWay"] == seen


def test_a_title_queued_while_its_reply_is_on_the_way_is_shown_queued() -> None:
    assert _run("queuedOnItsWay") == "Queued"
