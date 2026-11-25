"""The Watch page's Downloading row and the asking behind it, run under node against a fake DOM
with a clock that is stepped."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "watch_downloading_harness.js"


@pytest.fixture(scope="module")
def out() -> dict:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_with_nothing_downloading_there_is_no_row(out) -> None:
    assert out["nothing"] == {"hidden": True, "inside": 0}


def test_each_state_is_told_in_words(out) -> None:
    assert out["states"]["lines"] == {
        "Down": "71% · 6 min left",
        "Hours": "40% · about 2 h left",
        "Unknown": "40%",
        "Almost": "Almost ready",
        "Queued": "Queued",
        "Looking": "Looking for the best version",
        "Stuck": "Nothing found yet · next attempt in ~4h",
        "Ready": "Ready to watch",
        "Part": "Some episodes ready",
    }
    assert out["states"]["fill"] == "71"
    assert out["states"]["heading"] == "Downloading, 9, see all in Downloads"


def test_each_state_is_drawn_apart(out) -> None:
    classes = {
        k: set(v.split()) - {"watch-card", "dw"} for k, v in out["states"]["classes"].items()
    }
    assert classes["Down"] == set()
    assert classes["Queued"] == {"is-queued"}
    assert classes["Looking"] == {"is-looking"}
    assert classes["Stuck"] == {"is-looking", "is-stuck"}
    assert classes["Ready"] == {"is-ready"}


def test_a_card_opens_its_title_or_the_downloads_page_when_none_is_known(out) -> None:
    assert out["links"]["known"] == ["/watch#title=tmdb-movie-5", "tmdb-movie-5"]
    assert out["links"]["nzb"] == ["/downloads", None]


def test_addresses_that_are_not_mediamans_own_lead_nowhere(out) -> None:
    assert out["links"]["bad"] == ["/downloads", None]
    assert out["links"]["pictures"] == [None, None, None]


def test_a_card_stays_in_place_while_its_progress_moves(out) -> None:
    assert out["live"] == {"same": True, "line": "55% · 9 min left", "fill": "55"}


def test_the_only_card_left_takes_the_room(out) -> None:
    assert out["sole"] == {"before": False, "after": True, "cards": ["A"]}


def test_what_is_done_is_said_once_and_stays_a_minute(out) -> None:
    assert out["readyLands"]["at"] == {
        "said": "B is ready to watch",
        "cards": ["A", "B"],
        "recent": ["Salt", "Fieldwork"],
    }
    assert out["readyLands"]["still"] == ["A", "B"]


def test_then_it_goes_to_the_front_of_recently_added(out) -> None:
    landed = out["readyLands"]
    assert landed["cards"] == ["A"]
    assert landed["recent"] == ["B", "Salt", "Fieldwork"]
    assert landed["ref"] == "tmdb-movie-1"
    assert landed["heading"] == "Downloading, 1, see all in Downloads"


def test_it_has_its_thumb_and_its_eye_there_as_every_card_has(out) -> None:
    assert out["readyTools"] == {
        "B": ["tmdb-movie-9", "2", True, "movie/12", "true", True],
        "Cove": ["tmdb-movie-4", None, True, "tmdb-movie-4", "false", False],
    }


def test_it_goes_there_once_while_the_server_still_calls_it_ready(out) -> None:
    assert out["readyOnceOnly"] == {"cards": ["A"], "recent": ["B", "Salt", "Fieldwork"]}


def test_a_title_that_downloads_again_is_back_in_the_row(out) -> None:
    assert out["readyAgain"] == ["A", "B"]


def test_a_title_already_in_recently_added_is_not_put_there_twice(out) -> None:
    assert out["alreadyThere"] == ["Fieldwork", "Salt"]


def test_what_has_no_title_to_open_is_not_put_in_recently_added(out) -> None:
    assert out["readyWithoutTitle"] == {"hidden": True, "recent": ["Salt", "Fieldwork"]}


def test_the_row_goes_with_its_last_card_and_comes_back(out) -> None:
    assert out["lastOneCloses"] == {"open": True, "hidden": True, "inside": 0}
    assert out["closesAndComesBack"] == {"gone": True, "cards": ["C"], "hidden": False}


def test_a_row_on_its_way_out_comes_back_without_what_it_left_with(out) -> None:
    assert out["backWhileClosing"] == {"shut": True, "cards": ["C"], "open": True, "hidden": False}


def test_what_was_given_up_leaves_without_being_called_ready(out) -> None:
    assert out["abandoned"] == {"cards": ["B"], "recent": ["Salt", "Fieldwork"], "said": ""}


def test_the_focus_goes_on_to_the_next_card_or_to_where_the_title_went(out) -> None:
    assert out["focusMovesOn"] is True
    assert out["focusFollowsTheLast"] is True


def test_the_focus_goes_to_what_takes_the_last_cards_place(out) -> None:
    assert out["lastIsReplaced"] is True


def test_the_focus_does_not_go_to_a_card_that_leaves_too(out) -> None:
    assert out["focusSkipsWhatLeaves"] == {"passed": ["C"], "leaving": True, "onC": True}


def test_a_card_leaves_from_its_place_and_the_focus_goes_to_the_next(out) -> None:
    assert out["leavesFromItsPlace"] == {
        "ABC without A": {"row": "ABC", "moved": [], "focus": "B"},
        "ABC without B": {"row": "ABC", "moved": [], "focus": "C"},
        "ABCD without A": {"row": "ABCD", "moved": [], "focus": "B"},
        "ABCD without C": {"row": "ABCD", "moved": [], "focus": "D"},
    }


def test_the_focus_on_see_all_goes_to_the_next_row_when_the_row_closes(out) -> None:
    assert out["focusOnSeeAll"] == "movie-1"


def test_titles_ready_at_once_are_said_together(out) -> None:
    both = out["saysBoth"]
    assert (both["both"], both["then"]) == (
        "A and B are ready to watch",
        "C is ready to watch",
    )


def test_a_title_that_finishes_again_is_said_again(out) -> None:
    said = out["saidAgain"]
    assert said["first"] == said["again"] == "A is ready to watch"
    assert said["between"] == ""


def test_a_title_the_answer_let_go_of_may_be_ready_anew(out) -> None:
    assert out["readyAnew"] == {"gone": ["A"], "cards": ["B", "A"]}


def test_a_title_in_recently_added_under_another_ref_is_not_put_there_twice(out) -> None:
    assert out["underAnotherRef"] == ["tmdb-tv-4", "movie-2", "movie-1"]


def test_two_films_of_one_name_are_two_titles_there(out) -> None:
    assert out["twoFilmsOfOneName"] == ["B", "B", "Salt", "Fieldwork"]


def test_a_later_page_of_the_row_does_not_bring_the_title_again(out) -> None:
    assert out["loaderIsTold"] == ["B", "Salt", "Fieldwork", "Other"]


def test_a_title_taken_out_of_the_row_is_not_put_back(out) -> None:
    assert out["loaderHoldsIt"] == {"recent": ["Salt"], "cards": ["A"]}


def test_signed_out_it_stops_says_so_and_leaves_the_page_where_it_is(out) -> None:
    note = "You’re signed out. This is how it last was."
    assert out["signedOut"] == {
        "asks": 2,
        "at": "start",
        "cards": ["A"],
        "note": note,
        "held": True,
        # Asked once more as the tab is shown again, and goes on when signed in again.
        "shownAgain": [3, note],
        "signedIn": [None, False],
    }


def test_a_refusal_is_not_called_signed_out(out) -> None:
    note = "Can’t reach mediaman right now. This is how it last was."
    assert out["refused"] == {"asks": 2, "note": note}


def test_on_the_downloads_page_the_ring_is_told_and_asks_nothing(out) -> None:
    assert out["fed"] == {
        "asks": 0,
        "got": [[1, False], [2, False], [2, True], [0, False]],
    }


def test_a_new_card_takes_its_place_and_leaves_the_focus(out) -> None:
    assert out["reorder"] == {"cards": ["New", "A", "Q"], "focused": True}


def test_a_card_that_moves_keeps_the_focus(out) -> None:
    assert out["moved"] == {"cards": ["Q", "A"], "focused": True}


def test_what_the_server_stops_calling_ready_goes_to_recently_added_too(out) -> None:
    assert out["goneBeforeItsMinute"] == {"cards": ["A"], "recent": ["B", "Salt", "Fieldwork"]}


def test_a_service_that_does_not_answer_is_named_and_only_its_cards_dimmed(out) -> None:
    assert out["outage"]["first"] == {
        "note": "Can’t reach Radarr right now. This is how it last was.",
        "held": True,
        "other": False,
    }
    assert out["outage"]["second"] == "Can’t reach Radarr right now."
    assert out["outage"]["after"] is None


def test_one_failed_ask_is_no_news_and_two_keep_the_row_as_it_was(out) -> None:
    assert out["failed"]["once"] == {"cards": ["A"], "note": None}
    assert out["failed"]["twice"] == {
        "cards": ["A"],
        "note": "Can’t reach mediaman right now. This is how it last was.",
        "held": True,
    }
    assert out["failed"]["after"] == {"note": None, "held": False}
    assert out["failedFirst"] == {"first": True, "cards": ["A"]}


def test_it_asks_often_only_while_something_moves(out) -> None:
    pace = out["pace"]
    assert (pace["busy"], pace["idle"], pace["stuck"]) == (6, 2, 2)


def test_it_backs_off_when_it_fails_and_ends_when_refused(out) -> None:
    assert out["pace"]["refused"] == 0
    assert out["pace"]["failing"] <= 12


def test_a_hidden_tab_asks_nothing_and_asks_at_once_when_shown(out) -> None:
    assert out["hidden"] == {"during": 0, "back": 1}
    assert out["hiddenWhileAsking"] == {"aborted": 1, "cards": ["A"]}


def test_an_answer_that_never_comes_is_given_up(out) -> None:
    assert out["hung"] == {"given": 1, "cards": ["A"]}


def test_after_something_was_asked_for_it_looks_soon_and_for_a_while(out) -> None:
    assert out["justAsked"]["soon"] == 1
    assert out["justAsked"]["then"] >= 4


def test_the_page_is_drawn_with_the_answer_it_came_with(out) -> None:
    assert out["drawn"] == {"now": {"cards": ["A"], "open": True, "asks": 0}, "cards": ["Later"]}


def test_a_download_just_started_shows_soon_on_a_page_drawn_with_none(out) -> None:
    assert out["drawnIdle"] == {"asks": 0, "cards": ["New"]}


def test_an_old_answer_of_nothing_is_soon_asked_for_again_and_a_new_one_is_not(out) -> None:
    assert out["heldIdle"] == {"cards": ["New"], "asks": 1, "down": 1}


def test_all_readers_share_one_asking_and_one_that_breaks_stops_no_other(out) -> None:
    assert out["readers"] == {"got": [1, 2], "after": [1, 2, 2], "asks": 3, "last": 2}


def test_the_poster_lands_where_its_place_has_moved_to(out) -> None:
    assert out["follows"] == {
        "first": ["0px", "500px", "0"],
        "moved": "164px -140px",
        "below": "164px 420px",  # no further than just off the screen, 900px high
    }


def test_a_row_at_its_start_shows_what_comes_first_and_snaps_only_while_it_is_moved(out) -> None:
    assert out["staysAtStart"] == {
        "first": True,
        "held": False,
        "along": False,
        "back": True,
        "wheel": False,
        "wheelOver": True,
    }


def test_the_note_goes_out_of_reach_at_once_and_out_of_sight_as_it_has_closed(out) -> None:
    text = "Can’t reach Radarr right now."
    assert out["noteCloses"] == {
        "open": [False, False, True, text],
        "closing": [False, True, False, text],
        "shut": True,
    }
