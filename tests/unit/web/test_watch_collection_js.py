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


def test_the_row_comes_last_once_it_is_known_with_the_film_on_show_marked() -> None:
    out = _run("row")
    assert out["waiting"] is True
    assert out["gets"] == ["/api/watch/title/tmdb-movie-2/collection"]
    assert out["titles"] == [f"Film {n}" for n in range(1, 8)]
    assert out["last"] == "ttl__collsec"
    assert (out["here"], out["current"]) == (["Film 2"], "true")
    assert out["name"] == "The Saga · 7 films · 1 in the library"
    assert out["stops"] == [-1, 0, -1, -1, -1, -1, -1]
    assert out["get"] == "Download the missing 3"


def test_there_is_no_row_where_there_is_nothing_to_show() -> None:
    assert _run("nothing") == {"none": False, "alone": False, "failed": False, "show": 0}


def test_a_film_opens_its_page_and_back_finds_its_card() -> None:
    out = _run("open")
    # What the library holds opens as the library's; the film on show goes to its top.
    assert out["opened"] == [["tmdb-movie-3", "Film 3"], ["movie-41", "Film 1"]]
    assert out["focus"] == "film-tmdb-movie-3"
    assert out["top"] == "ttl-title"
    assert out["atOnce"] == ["Film 3"]
    assert out["back"] is True


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
    assert (out["go"], out["left"]) == ("Try again: download 1", "Download the missing film")
    assert out["again"] == [2, 3, 4, 3]
    assert out["then"] == ["Done", None]
    # What opened the dialog has gone, so the focus is on the row's heading.
    assert (out["closed"], out["focus"]) == (True, "coll-title")


def test_at_the_limit_the_rest_is_not_asked_for_until_tried_again() -> None:
    out = _run("limited")
    later = "Not asked yet, try again in a minute"
    assert out["ids"] == [2, 3]
    assert out["rows"][1:] == [f"Film 3: 2003 · {later}", f"Film 4: 2004 · {later}"]
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
