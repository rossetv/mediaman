"""The Watch page's Discover rows, Recommended row and Keep from Leaving soon, run under node
against a fake DOM."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

STATIC = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "static"
HARNESS = Path(__file__).parent / "js" / "watch_browse_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_skeleton_gives_way_to_the_discover_rows_with_their_badges() -> None:
    out = _run("loaded")
    assert out["rows"] == ["recommended", "trending", "popular_movies", "popular_tv"]
    assert (out["skeleton"], out["busy"], out["label"]) == (False, None, "Discover")
    assert out["badges"] == ["In library", "Queued", "Downloading 42%", None]


def test_cards_open_by_title_ref_keep_a_link_and_drop_a_bad_ref() -> None:
    out = _run("loaded")
    assert out["refs"] == ["movie-11", "tmdb-movie-2", "tmdb-tv-3", "tmdb-movie-4"]
    assert out["hrefs"][0] == "/watch/movie/11"
    assert out["hrefs"][1] == "/watch#title=tmdb-movie-2"


def test_each_card_carries_its_kind_for_the_detail_to_show_while_loading() -> None:
    kinds = dict(_run("loaded")["kinds"])
    assert (kinds["rec-7"], kinds["tmdb-tv-3"], kinds["tmdb-movie-2"]) == ("movie", "show", "movie")


def test_a_title_from_the_server_is_text_never_markup() -> None:
    assert _run("loaded")["hostileTitle"] == [3]


def test_rows_added_after_load_get_the_row_arrows_once() -> None:
    assert _run("loaded")["arrows"] == [2, 2, 2, 2]


def test_a_title_change_updates_the_badge_on_its_cards() -> None:
    out = _run("titleChanged")
    assert out == {"queued": "Queued", "downloading": "Downloading 42%", "inLibrary": "In library"}


def test_a_title_change_reaches_the_pick_of_the_same_title_kind_and_year() -> None:
    out = _run("titleChangedOnAnotherRef")
    assert out["queued"] == ["Queued", "Queued", None, None]
    assert out["downloading"] == ["Downloading 42%", "Downloading 42%", None, None]


def test_a_failed_discover_row_says_which_and_try_again_loads_it_in_place() -> None:
    out = _run("discoverFailed")
    assert out["notes"][0].startswith("Couldn’t load Trending this week.")
    assert len(out["notes"]) == 3
    assert out["rec"] is True
    assert out["after"] == ["recommended", "trending", "popular_movies", "popular_tv"]
    assert (out["failedLeft"], out["cards"]) == (0, 4)


def test_without_tmdb_or_picks_the_slot_is_emptied_and_tmdb_is_not_asked() -> None:
    out = _run("nothing")
    assert out["children"] == 0
    assert out["calls"] == ["GET /api/watch/recommended"]


def test_recommendations_off_hide_the_row_from_viewers_and_point_admins_to_settings() -> None:
    out = _run("recsOff")
    assert out["viewerRow"] is False
    assert out["viewerRows"] == ["trending", "popular_movies", "popular_tv"]
    assert out["adminLink"] == "/settings#setg-general"
    assert out["adminTools"] == 0


def test_no_picks_says_so_and_only_admins_get_the_refresh_hint() -> None:
    out = _run("noPicks")
    assert out["viewer"] == "No recommendations yet."
    assert out["admin"].startswith("No recommendations yet.")
    assert "Refresh" in out["admin"]


def test_viewers_can_switch_batches_but_only_admins_can_refresh() -> None:
    out = _run("tools")
    assert out["viewer"] == ["picks"]
    assert out["admin"] == ["picks", "refresh"]


def test_choosing_an_earlier_batch_swaps_the_picks_in_the_same_row() -> None:
    out = _run("batch")
    assert out["open"]["keepMenuLook"] is True
    assert out["open"]["items"] == [["Latest · 21 Nov", "true"], ["14 Nov", "false"]]
    assert out["open"]["focus"] is True
    assert out["sameRow"] is True
    assert out["cards"] == ["rec-9"]
    assert out["checked"] == ["14 Nov"]
    assert "14 Nov" in out["pill"]


def test_refresh_during_the_cooldown_shows_the_time_left_and_no_error() -> None:
    out = _run("refreshCooldown")
    assert out["label"] == "Refresh in 14 h"
    assert out["disabled"] == "true"
    assert out["error"] is False
    assert out["note"] == "Shared picks can be fetched again in 14 h."


def test_a_cooling_refresh_says_how_long_when_pressed_and_keeps_its_width() -> None:
    out = _run("refreshCooldown")
    assert out["pressed"] == ["Shared picks can be fetched again in 14\u00a0h."]
    assert out["widest"] == "Refresh in 59\u00a0min"
    css = (STATIC / "css" / "_watch_browse.css").read_text(encoding="utf-8")
    assert re.search(r"\.row-tool--refresh::before \{ content: attr\(data-widest\)", css)


def test_a_refused_refresh_says_why_without_moving_the_row_and_can_be_tried_again() -> None:
    out = _run("refreshFailed")
    assert out["toasts"] == [["Plex not configured", "error"]]
    assert out["moved"] == 0
    assert (out["label"], out["disabled"]) == ("Refresh shared", None)


def test_a_session_lost_before_the_rows_load_goes_to_login_not_an_error() -> None:
    out = _run("loggedOutOnLoad")
    assert out == {"href": "/login", "failed": 0}


def test_a_long_picks_menu_stays_on_screen_and_scrolls_to_the_focused_batch() -> None:
    out = _run("longPicksMenu")
    assert out["top"] >= 0
    assert out["bottom"] <= 500  # above the pill, the side with the room
    assert out["capped"] and float(out["capped"].removesuffix("px")) < out["full"]
    assert out["lastFocused"] is True
    assert out["menuTop"] <= out["lastTop"]
    assert out["lastBottom"] <= out["menuBottom"]


def test_a_session_lost_while_refreshing_goes_to_login() -> None:
    assert _run("refreshLoggedOut")["href"] == "/login"


def test_a_finished_refresh_puts_the_new_picks_in_the_same_row_and_starts_the_cooldown() -> None:
    out = _run("refreshDone")
    assert out["busy"] == {"label": "Refreshing…", "spinning": True}
    assert out["sameRow"] is True
    assert out["cards"] == ["rec-20"]
    assert out["after"] == "Refresh in 24 h"


def test_a_person_s_own_picks_open_by_their_own_refs_marked_as_ai_picks() -> None:
    out = _run("ownRow")
    # An older batch repeats a title under another number, and lists a ref that is none.
    assert out["refs"] == ["movie-11", "pick-2", "rec-7", "pick-31"]
    assert out["picks"] == ["pick-1", "pick-2", "rec-7", "pick-31"]
    assert out["marked"] == ["AI pick,"] * 4
    assert out["chip"] == "AI picks"
    assert out["line"]["text"] == "Made from what you’ve played, then new and popular."
    # The mark is no part of what a card of the same title is found by.
    assert out["updated"] == "Queued"


def test_the_line_says_why_there_are_no_new_picks_to_be_had() -> None:
    out = _run("ownStates")
    assert out["wait"]["button"] == "Refresh in 6 days"
    assert out["wait"]["toasts"] == ["Your picks can be refreshed again in 6\u00a0days."]
    assert (out["limit"]["button"], out["limit"]["off"]) == ("Refresh in 5 h", True)
    assert out["limit"]["text"] == "That’s all the refreshes for now."
    assert (out["few"]["text"], out["few"]["button"]) == (
        "Play five titles and these become yours.",
        None,
    )
    assert (out["noKeyViewer"]["text"], out["noKeyViewer"]["button"]) == ("New and popular.", None)
    assert "OpenAI key" in out["noKeyAdmin"]["text"]
    assert {v["posts"] for k, v in out.items() if k != "off"} == {0}
    assert out["off"] is False


def test_a_person_has_their_own_picks_made_and_they_come_in_place() -> None:
    out = _run("ownRefreshDone")
    assert out["before"]["button"] == "Refresh my picks"
    assert (out["busy"]["button"], out["busy"]["spinning"]) == ("Refreshing…", True)
    assert out["posts"] == ["POST /api/watch/picks/refresh"]
    assert out["refs"] == ["movie-11", "pick-2", "rec-7"]
    assert out["after"]["button"] == "Refresh in 6 days"
    assert (out["sameRow"], out["focus"], out["toasts"]) == (True, True, ["Your new picks are in."])


def test_own_picks_that_could_not_be_made_leave_the_row_and_can_be_tried_again() -> None:
    out = _run("ownRefreshFailed")
    assert out["failed"]["text"].startswith("Couldn’t make new ones")
    assert (out["failed"]["button"], out["failed"]["off"]) == ("Refresh my picks", False)
    assert out["failed"]["variant"] == "error"
    assert out["sameCards"] is True
    assert out["again"]["text"].startswith("Making yours")


def test_a_refused_refresh_of_own_picks_says_why_and_keeps_the_focus() -> None:
    out = _run("ownRefused")
    assert (out["wait"]["button"], out["wait"]["focus"]) == ("Refresh in 6 days", "row-tool")
    assert out["few"]["text"] == "Play five titles and these become yours."
    assert (out["few"]["button"], out["few"]["focus"]) == (None, "watch-row__scroll")
    assert out["running"]["button"] == "Refreshing…"
    assert (out["broken"]["button"], out["broken"]["toasts"]) == ("Refresh my picks", 1)
    assert out["out"]["href"] == "/login"


def test_own_picks_being_made_when_the_page_loads_are_followed_to_their_end() -> None:
    out = _run("ownRunningAtLoad")
    assert out["made"]["busy"].startswith("Making yours")
    assert out["made"]["toasts"] == ["Your new picks are in."]
    assert out["none"]["toasts"] == []
    assert out["none"]["after"].startswith("Made from what you’ve played")
    assert len(out["failed"]["toasts"]) == 1


def test_keep_from_leaving_soon_offers_undo_and_moves_focus_to_the_next_keep() -> None:
    out = _run("keepFromRow")
    assert out["gone"] is True
    assert out["focus"] == "1101"
    assert out["toast"] == "Kept Leaving Film for 30 days."
    assert out["undo"] == "Undo"


def test_landing_from_recommended_scrolls_its_row_into_view_and_cleans_the_address() -> None:
    out = _run("landOnRecommended")
    # The marker goes before anything reads the address; an old pick link's hash stays.
    assert out["landed"]["replacedAtOnce"] == ["/watch#recommendation-5"]
    assert out["landed"]["scrolls"] == [{"top": 958, "behavior": "auto"}]
    assert out["withMotion"]["replacedAtOnce"] == ["/watch?view=rows"]
    assert out["withMotion"]["scrolls"] == [{"top": 958, "behavior": "smooth"}]
    assert out["plain"] == {"replacedAtOnce": [], "scrolls": []}
    # No Recommended row for a viewer with picks off, and nobody is pulled back from where they scrolled.
    assert out["picksOff"] == {"replacedAtOnce": ["/watch"], "scrolls": []}
    assert out["alreadyScrolled"]["scrolls"] == []


def test_a_season_line_of_pills_pages_by_the_keys_and_the_arrow() -> None:
    assert _run("seasonLine") == {"hasNext": True, "afterKey": 100, "afterArrow": 500}


def test_a_row_loads_page_after_page_to_the_last_and_never_shows_a_title_twice() -> None:
    out = _run("pagesToTheEnd")
    assert out["calls"] == [2, 3, 4]
    # Two titles of one name are two titles when their TMDB ids differ.
    assert out["refs"] == [f"tmdb-movie-{n}" for n in range(1, 6)]
    assert (out["things"], out["more"], out["next"]) == (5, False, False)


def test_one_page_is_asked_for_at_a_time_with_placeholders_and_next_still_shown() -> None:
    out = _run("pageOnItsWay")
    assert out["far"] == {"calls": 0}
    assert out["waiting"] == {
        "calls": [2],
        "things": 16,
        "busy": "true",
        "hidden": True,
        "next": True,
    }
    assert (out["kept"], out["cards"], out["things"], out["busy"]) == (True, 30, 30, None)


def test_a_row_with_plenty_left_to_scroll_asks_for_nothing() -> None:
    assert _run("pageNotNear") == []


def test_pages_of_nothing_but_repeats_end_the_row_instead_of_looping() -> None:
    assert _run("pagesOfRepeats") == {"calls": 8, "things": 1, "more": False}


def test_a_page_that_fails_offers_try_again_and_waits_for_it() -> None:
    out = _run("pageFailed")
    assert out["failed"] == {
        "calls": [2],
        "things": 2,
        "text": "Couldn\u2019t load more.Try again",
        "toasts": 0,
        "next": True,
    }
    assert out["still"]["calls"] == [2, 2]
    assert len(out["still"]["toasts"]) == 1
    assert (out["still"]["busy"], out["still"]["focus"]) == ("false", True)


def test_next_at_the_end_tries_a_failed_page_again_and_focus_goes_on_to_the_new_cards() -> None:
    out = _run("pageFailed")
    assert out["refs"] == ["tmdb-movie-1", "tmdb-movie-2", "tmdb-movie-3"]
    assert out["calls"] == [2, 2, 2, 3]
    assert out["focus"] == "tmdb-movie-2"
    assert out["tiles"] == 1


def test_a_row_asked_too_often_waits_longer_each_time_then_offers_try_again() -> None:
    out = _run("pageTooMany")
    assert out["quiet"] == {"tile": False, "skel": 6}
    assert out["calls"] == 1
    assert out["waits"] == sorted(out["waits"]) and len(out["waits"]) == 5
    assert (out["total"], out["tile"], out["toasts"]) == (6, True, 0)


def test_posters_far_off_the_screen_are_unloaded_and_come_back() -> None:
    out = _run("farPosters")
    poster = "/api/watch/img/tmdb/w342/a.jpg"
    assert out["atStart"] == [poster, None]
    assert out["atEnd"] == [None, poster]
    assert (out["back"], out["cards"]) == (poster, 100)


def test_the_picks_go_on_to_older_batches_from_the_one_the_row_starts_at() -> None:
    out = _run("olderPicks")
    # rec-10 is rec-7 again; the batch after it is gone, so the row ends.
    assert out["first"] == {"refs": ["rec-7", "rec-8", "rec-9"], "more": False}
    assert out["chosen"] == ["rec-9", "rec-11"]
    assert "14 Nov" in out["pill"]


def test_an_older_batch_on_its_way_when_the_row_starts_again_is_not_added() -> None:
    assert _run("refreshedWhileLoading") == {"old": ["rec-7", "rec-8"], "now": ["rec-30"]}


def test_focus_moves_into_the_row_that_try_again_brought_back() -> None:
    assert _run("focusAfterRetry") == {"discover": "popular_tv", "recommended": "recommended"}


def test_a_season_line_goes_by_where_each_pill_stands_however_wide() -> None:
    out = _run("unevenSeasonLine")
    # The fourth pill (500-760) is cut off at 700; the seventh (960-1360) at 1200; the line ends at 1460.
    assert out["arrows"] == [500, 760, 760, 60, 0]
    assert out["keys"] == [100, 400, 100]


def test_a_library_row_loads_on_from_the_page_s_cards_to_its_end() -> None:
    out = _run("libraryRow")
    assert out["refs"] == ["show-1", "show-2", "show-3", "show-4"]
    assert out["calls"] == [
        "GET /api/watch/library/tv/row?after=a%3D1",
        "GET /api/watch/library/tv/row?after=b2",
    ]
    assert out["more"] is False
    # As the page draws it: its mark and More, and no Keep outside Leaving soon.
    assert out["third"] == ["show/3", True, True, False]


def test_a_discover_card_has_a_mark_and_more_only_for_a_title_of_the_library() -> None:
    assert _run("discoverMarks") == [
        ["movie/11", True, 1, False],
        [None, False, 0, False],
        [None, False, 0, False],
    ]
