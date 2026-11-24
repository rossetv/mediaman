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


def test_nothing_opens_under_an_open_menu_or_the_detail() -> None:
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
