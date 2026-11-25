"""The films of a film's collection, in its detail, run under node against a fake DOM: the row,
and the dialog that asks for those that are missing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_collection_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_row_comes_once_it_is_known_with_the_film_on_show_marked() -> None:
    out = _run("row")
    assert out["waiting"] is True
    assert out["gets"] == ["/api/watch/title/tmdb-movie-2/collection"]
    assert out["titles"] == [f"Film {n}" for n in range(1, 8)]
    assert out["last"] == "ttl__collsec"
    assert (out["here"], out["current"]) == (["Film 2"], "true")
    assert out["name"] == "The Saga · 7 films · 1 in the library"
    assert out["stops"] == [-1, 0, -1, -1, -1, -1, -1]
    assert (out["get"], out["brief"]) == ("Download the missing 3", "Get 3 missing")


def test_there_is_no_row_where_there_is_nothing_to_show() -> None:
    assert _run("nothing") == {"none": False, "alone": False, "failed": False, "show": 0}


def test_the_place_of_the_films_is_held_until_they_are_known() -> None:
    out = _run("expected")
    for got in out.values():
        assert (got["before"], got["held"], got["again"]) == (False, [True, True, "true"], True)
        assert got["skeleton"] is False
    assert [(g["films"], g["shown"], g["next"]) for g in out.values()] == [
        # Only a film known to be in one has its place held the next time, while it loads.
        (7, True, True),
        (0, False, False),
        (0, False, False),
    ]


def test_a_film_opens_its_page_and_back_finds_its_card() -> None:
    out = _run("open")
    # What the library holds opens as the library's; the film on show goes to its top.
    assert out["opened"] == [["tmdb-movie-3", "Film 3"], ["movie-41", "Film 1"]]
    assert out["focus"] == "film-tmdb-movie-3"
    assert out["top"] == "ttl-title"
    assert out["atOnce"] == ["Film 3"]
    assert out["back"] is True


def test_a_card_s_buttons_are_reached_with_the_arrow_down_and_are_no_tab_stops() -> None:
    assert _run("tools") == {"stops": 0, "at": True, "own": False}


def test_the_arrow_keys_go_along_the_row_and_stop_at_its_ends() -> None:
    assert _run("keys") == [
        "film-tmdb-movie-3",
        "film-tmdb-movie-7",
        "film-tmdb-movie-7",
        "film-tmdb-movie-1",
        "film-tmdb-movie-1",
    ]


def test_the_row_stays_as_it_is_when_its_title_is_drawn_again_and_follows_a_mark() -> None:
    assert _run("drawnAgain") == {"same": True, "gets": 1, "then": 2}


def test_nothing_is_offered_where_none_is_missing() -> None:
    assert _run("noneMissing") == {"row": True, "get": None}


def test_the_dialog_lists_what_is_missing_and_says_what_is_left_out_and_why() -> None:
    out = _run("confirm")
    assert out["open"] is True
    assert out["rows"] == ["Film 2: 2002", "Film 3: 2003", "Film 4: 2004"]
    assert out["boxes"] == [True, True, True]
    assert out["out"] == [
        "Already in the library: Film 1",
        "Already asked for: Film 5",
        "Not out yet: Film 6 (12 Jul 2027)",
        "No release date yet: Film 7",
    ]
    assert (out["go"], out["fewer"], out["posts"]) == ("Download 3", "Download 2", 0)
    assert out["empty"] == ["true", 0, True]


def test_the_ticked_films_are_asked_for_one_at_a_time_and_once_though_pressed_twice() -> None:
    out = _run("oneByOne")
    first = out["first"]
    assert (first["ids"], first["disabled"]) == ([2], "true")
    assert first["go"] == "Adding 1 of 2…"
    assert out["second"] == [2, 4]
    assert out["sent"] == [
        ["/api/search/download", {"media_type": "movie", "tmdb_id": n, "title": f"Film {n}"}]
        for n in (2, 4)
    ]
    assert out["rows"] == ["Film 2: 2002 · Queued", "Film 3: 2003", "Film 4: 2004 · Queued"]
    assert out["said"] == "2 films are queued."
    assert out["left"] == "Download the missing film"


def test_each_film_added_is_told_to_the_page_as_a_single_download_is() -> None:
    event = _run("oneByOne")["events"][0]
    assert event == {
        "ref": "tmdb-movie-2",
        "download_state": "queued",
        "download_pct": None,
        "title": "Film 2",
        "kind": "movie",
        "year": 2002,
    }


def test_a_failure_is_told_at_its_film_and_only_that_one_is_tried_again() -> None:
    out = _run("partial")
    assert out["rows"] == [
        "Film 2: 2002 · Already in Radarr",
        "Film 3: 2003 · Failed to add to Radarr",
        "Film 4: 2004 · Queued",
    ]
    assert out["said"] == "2 of 3 films are queued. 1 was not."
    assert (out["go"], out["left"]) == ("Try again with 1", "Download the missing film")
    assert out["again"] == [2, 3, 4, 3]
    assert out["then"] == ["Done", None]
    # What opened the dialog has gone, so the focus is on the row's heading.
    assert (out["closed"], out["focus"]) == (True, "coll-title")


def test_at_the_limit_the_rest_is_not_asked_for_until_tried_again() -> None:
    out = _run("limited")
    assert out["ids"] == [2, 3]
    assert out["rows"][1:] == ["Film 3: 2003 · Not asked yet", "Film 4: 2004 · Not asked yet"]
    assert out["red"] == [False, False, False]
    # The server does not say whether the limit is the minute's or the day's.
    assert out["said"] == (
        "1 of 3 films is queued. 2 are not asked for yet: "
        "try again in a minute, or tomorrow if the day’s limit is reached."
    )
    assert out["again"] == [2, 3, 3]


def test_a_repeat_of_one_films_request_does_not_stop_the_rest() -> None:
    out = _run("duplicate")
    assert out["ids"] == [2, 3]
    assert "Duplicate request" in out["row"]


def test_closing_while_films_are_added_lets_them_go_on_and_opening_shows_them() -> None:
    out = _run("closed")
    assert (out["open"], out["running"]) == (False, "Adding 1 of 3…")
    assert out["second"] == [2, 3]
    assert out["reopened"] == [
        "Film 2: 2002 · Queued",
        "Film 3: 2003 · Adding…",
        "Film 4: 2004 · Waiting",
    ]
    assert out["ids"] == [2, 3, 4]
    assert out["toasts"] == [["3 films are queued.", "success"]]
    assert out["get"] is None


def test_a_film_just_added_is_not_offered_again_before_radarr_lists_it() -> None:
    assert _run("grace") == {"soon": None, "later": "Download the missing 3"}


def test_a_film_downloaded_elsewhere_is_no_longer_missing() -> None:
    assert _run("elsewhere") == {"get": "Download the missing 2"}


def test_the_title_on_show_is_told_by_its_own_ref_too() -> None:
    assert _run("pick") == {"refs": ["tmdb-movie-2", "rec-9"]}


def test_a_title_closed_under_the_dialog_does_not_take_the_focus_back() -> None:
    assert _run("closedByBack") == {"open": False, "kept": True}


def test_a_title_opened_again_has_its_row_drawn_as_it_now_is() -> None:
    out = _run("reopened")
    assert out["drawn"] is True
    assert out["badges"] == [False, True, False, False, True, False, False]
    assert out["count"] == 7


def test_leaving_the_page_is_asked_about_while_films_are_left_to_ask_for() -> None:
    out = _run("unload")
    assert out["out"] == [False, True, False, False]
    assert "Keep this page open" in out["said"]
    assert out["listening"] == 0


def test_a_second_collection_waits_for_the_first() -> None:
    out = _run("another")
    assert out["get"] == "Download the missing 2"
    assert (out["open"], out["toast"], out["ids"]) == (False, 1, [2])


def test_a_film_that_failed_is_left_without_a_spinner_and_can_be_ticked_after() -> None:
    out = _run("spinner")
    assert out["adding"] == [True, True, True]
    assert out["failed"] == [False, True, True]
    assert out["after"] == [False, False, False]


def test_an_older_answer_does_not_replace_a_newer_one() -> None:
    assert _run("older")["name"].startswith("New")


def test_the_rest_of_a_double_click_neither_closes_the_dialog_nor_asks_again() -> None:
    assert _run("doubleClick") == {"open": True, "ids": [2, 3, 4], "stays": True, "closed": True}


def test_the_row_is_found_where_it_was_left() -> None:
    assert _run("place") == {"other": False, "back": 340, "fresh": True}


def test_a_film_got_elsewhere_meanwhile_is_done_without_being_asked_for() -> None:
    out = _run("meanwhile")
    assert out["ids"] == [2]
    assert out["rows"][1:] == [
        "Film 3: 2003 · Already asked for",
        "Film 4: 2004 · Already in the library",
    ]
    assert (out["said"], out["go"]) == ("3 films are queued or in the library.", "Done")


def test_a_failed_refresh_keeps_the_row_and_is_tried_again() -> None:
    assert _run("outage") == {"kept": True, "gets": 3, "name": "Back again"}


def test_a_film_taken_out_again_before_its_turn_is_asked_for() -> None:
    assert _run("takenOut") == {"ids": [2, 3], "row": "Film 3: 2003 · Adding…"}


def test_a_skipped_film_counts_in_the_progress() -> None:
    adding = "Adding 3 of 3…"
    assert _run("skipped") == {"ids": [2, 4], "go": adding, "get": adding}


def test_what_failed_is_still_told_when_the_dialog_is_opened_again() -> None:
    out = _run("failedClosed")
    assert out["spins"] is True
    assert out["rows"] == [
        "Film 2: 2002 · Queued",
        "Film 3: 2003 · Failed to add to Radarr",
        "Film 4: 2004 · Queued",
    ]
    assert (out["said"], out["go"]) == (
        "2 of 3 films are queued. 1 was not.",
        "Try again with 1",
    )
    assert out["ids"] == [2, 3, 4, 3]
    # Got another way it is no failure any more, and what was taken out is missing again.
    assert out["later"] == [["Film 2: 2002"], "Download 1"]


def test_a_session_that_ends_stops_the_run_without_a_question_about_leaving() -> None:
    out = _run("signedOut")
    assert (out["before"], out["unsaved"], out["after"]) == (True, False, False)
    assert [r.split(" · ")[1] for r in out["rows"]] == ["Not asked yet"] * 3
    assert (out["go"], out["disabled"]) == ("Try again with 3", None)
    assert out["ids"] == [2, 2]
    assert out["still"] == [2, 2]


def test_enter_held_down_does_not_press_what_comes_next() -> None:
    assert _run("held") == {"prevented": [False, True], "soon": True, "later": False}


def test_all_films_are_unticked_or_ticked_at_once_while_none_is_added() -> None:
    out = _run("toggleAll")
    assert out["out"] == [
        ["Select none", [True, True, True], "Download 3"],
        ["Select all", [False, False, False], "Pick films"],
        ["Select all", [False, True, False], "Download 1"],
        ["Select none", [True, True, True], "Download 3"],
    ]
    assert out["busy"] is True
    assert out["ids"] == [2, 3, 4]
    # One left to tick needs no such button.
    assert out["one"] is True


def test_the_row_stays_at_its_start_where_the_film_is_whole_in_view() -> None:
    assert _run("whereItStands") == [0, 0, 247]
