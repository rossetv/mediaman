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


def test_a_row_the_server_has_more_of_opens_as_a_grid_that_loads_the_rest() -> None:
    out = _run("libraryGrids")
    assert out == {"recent": [9, "z"]}


def test_a_card_of_a_held_title_that_is_leaving_says_when() -> None:
    assert _run("leavingCards") == [[["11", "2 days left", 1], [None, None, 0]]] * 2


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


def test_no_picks_says_who_the_first_are_for_and_sends_an_admin_to_make_them() -> None:
    out = _run("noPicks")
    words = "No picks yet. The first ones are for everyone, until your own are made."
    assert out["viewer"] == [words, None]
    assert out["admin"] == [words, "/settings#setg-integrations"]
    assert out["noKey"] == [words, None]


def test_the_row_has_one_refresh_the_person_s_own_for_everyone_and_asks_for_no_other() -> None:
    assert _run("tools") == [[[True], ["Refresh my picks"], []]] * 3


def test_the_picks_button_names_the_batch_at_the_row_s_left_as_it_is_scrolled() -> None:
    out = _run("follows")
    assert (out["start"], out["refs"]) == ([["Latest picks · 21 Nov", "Latest"]], 15)
    # Most of three cards from the left: 1.6 of one batch and 1.4 of the other, then the turn.
    assert out["halfway"] == [["Latest picks · 21 Nov", "Latest"]]
    assert out["over"] == [["Picks of 14 Nov", "14 Nov"]]
    assert out["last"] == [["Picks of 7 Nov", "7 Nov"]]
    assert out["checked"] == ["Picks of 7 Nov"]
    assert out["back"] == [["Latest picks · 21 Nov", "Latest"]]
    assert out["asked"] == 0
    assert (out["label"], out["live"]) == ("Picks: Picks of 7 Nov. Choose another batch", False)


def test_the_picks_button_is_as_wide_as_its_widest_name_and_fades_between_them() -> None:
    assert _run("follows")["sizers"] == [
        "Latest picks · 21 Nov",
        "Picks of 14 Nov",
        "Picks of 7 Nov",
    ]
    css = (STATIC / "css" / "_watch_browse.css").read_text(encoding="utf-8")
    assert ".picks-name > * { grid-area: 1 / 1; }" in css
    assert ".picks-name__one { opacity: 0; transition: opacity var(--dur)" in css
    out = _run("follows")
    assert out["still"] == out["fading"]  # nothing is drawn again for the same name
    assert out["faded"] == 1


def test_choosing_a_date_scrolls_the_same_row_to_it_and_back_to_the_latest() -> None:
    out = _run("chooses")
    assert out["before"] == 40
    assert (out["loaded"], out["unique"]) == (120, 120)
    assert (out["at"], out["name"], out["busy"]) == (80, [["Picks of 7 Nov", "7 Nov"]], None)
    assert out["onTheWay"] == [["Picks of 7 Nov", "7 Nov"]]
    assert out["theirs"] == [["Latest picks · 21 Nov", "Latest"]]
    assert out["latest"] == [0, [["Latest picks · 21 Nov", "Latest"]], 0, True, True]


def test_a_date_that_cannot_be_loaded_says_so_and_leaves_the_row_where_it_was() -> None:
    for lost in _run("choosesLost"):
        assert lost == {
            "name": [["Latest picks · 21 Nov", "Latest"]],
            "left": 0,
            "busy": None,
            "toasts": ["Couldn’t load those picks. Try again."],
        }


def test_a_date_whose_titles_are_all_elsewhere_goes_to_the_next_older_one() -> None:
    assert _run("choosesEmptied") == {"name": [["Picks of 7 Nov", "7 Nov"]], "at": "rec-180"}


def test_a_row_scrolled_while_a_chosen_date_is_on_its_way_stays_with_the_person() -> None:
    assert _run("choosesLeft") == {
        "busy": "true",
        "after": None,
        "left": 5,
        "name": [["Latest picks · 21 Nov", "Latest"]],
    }


def test_a_first_batch_without_a_date_says_latest_with_no_dangling_separator() -> None:
    out = _run("undatedFirstBatch")
    assert out["items"] == ["Latest picks", "Picks of 14 Nov"]
    assert out["pill"] == "Latest picks"
    # A phone does not show the long part.
    assert out["phone"] == "Latest"
    assert out["label"] == "Picks: Latest picks. Choose another batch"


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


def test_a_person_s_own_picks_open_by_their_own_refs_marked_as_ai_picks() -> None:
    out = _run("ownRow")
    # An older batch repeats a title under another number, and lists a ref that is none.
    assert out["refs"] == ["movie-11", "pick-2", "rec-7", "pick-31"]
    assert out["picks"] == ["pick-1", "pick-2", "rec-7", "pick-31"]
    assert out["marked"] == ["AI pick,"] * 4
    assert out["chip"] == "AI picks"
    assert out["line"]["text"] == "From your plays, then new and popular."
    # The mark is no part of what a card of the same title is found by.
    assert out["updated"] == "Queued"


def test_the_line_says_why_there_are_no_new_picks_to_be_had() -> None:
    out = _run("ownStates")
    assert out["wait"]["button"] == "Refresh in 6 days"
    assert out["wait"]["toasts"] == ["Your picks can be refreshed again in 6\u00a0days."]
    assert (out["limit"]["button"], out["limit"]["off"]) == ("Refresh in 5 h", True)
    assert out["limit"]["text"] == "That’s all the refreshes for now."
    assert (out["few"]["text"], out["few"]["button"]) == (
        "For everyone. Play five titles and these become yours.",
        None,
    )
    assert (out["noKeyViewer"]["text"], out["noKeyViewer"]["button"]) == ("For everyone.", None)
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


def test_the_words_change_while_picks_are_made_and_a_screen_reader_is_told_once() -> None:
    out = _run("ownMaking")["made"]
    assert (out["before"]["shown"], out["before"]["reel"], out["before"]["twinkles"]) == (
        None,
        False,
        False,
    )
    assert (out["busy"]["heard"], out["busy"]["role"], out["busy"]["reel"]) == (
        False,
        "status",
        True,
    )
    assert out["busy"]["shown"] and out["busy"]["twinkles"]
    assert out["later"]["shown"] != out["busy"]["shown"]
    assert out["starts"] == 1
    assert out["told"] == ["Making your picks. It can take a minute or two."]


def test_the_end_shows_here_they_are_then_the_usual_words_and_is_told_by_the_toast_alone() -> None:
    out = _run("ownMaking")["made"]
    assert (out["end"]["shown"], out["end"]["live"], out["beating"]) == ("Here they are.", "off", 0)
    assert (out["after"]["shown"], out["after"]["heard"]) == (None, True)
    assert out["after"]["status"] == "From your plays, then new and popular."
    assert (out["after"]["twinkles"], out["after"]["reel"]) == (False, False)
    assert out["toasts"] == ["Your new picks are in."]


def test_a_failure_says_its_own_words_at_once() -> None:
    out = _run("ownMaking")["failed"]
    assert (out["end"]["shown"], out["end"]["heard"], out["end"]["live"]) == (None, True, None)
    assert out["end"]["status"] == "Couldn’t make new ones. Try again."
    assert out["beating"] == 0


def test_picks_made_again_at_once_go_on_without_an_end() -> None:
    out = _run("ownMaking")["again"]
    assert out["end"]["shown"] not in (None, "Here they are.")
    assert (out["end"]["twinkles"], out["beating"]) == (True, 1)


def test_a_press_that_fails_after_one_that_made_picks_has_no_happy_end() -> None:
    out = _run("ownMakingThenBroken")
    assert out["first"] == "Here they are."
    assert out["busy"]["shown"] not in (None, "Here they are.")
    assert (out["busy"]["heard"], out["second"], out["heard"]) == (False, None, True)


def test_someone_who_rated_few_titles_is_told_so_kindly() -> None:
    source = (STATIC / "js" / "watch" / "making.js").read_text(encoding="utf-8")
    few = re.search(r"var FEW = \[(.*?)\];", source, re.DOTALL)
    assert few and f"'{_run('ownMaking')['few']}'" in few[1]


def test_own_picks_that_could_not_be_made_leave_the_row_and_can_be_tried_again() -> None:
    out = _run("ownRefreshFailed")
    assert out["failed"]["text"].startswith("Couldn’t make new ones")
    assert (out["failed"]["button"], out["failed"]["off"]) == ("Refresh my picks", False)
    assert out["failed"]["variant"] == "error"
    assert out["sameCards"] is True
    assert out["again"]["text"].startswith("Making your picks")


def test_a_refused_refresh_of_own_picks_says_why_and_keeps_the_focus() -> None:
    out = _run("ownRefused")
    assert (out["wait"]["button"], out["wait"]["focus"]) == ("Refresh in 6 days", "row-tool")
    assert out["few"]["text"] == "For everyone. Play five titles and these become yours."
    assert (out["few"]["button"], out["few"]["focus"]) == (None, "watch-row__scroll")
    assert out["running"]["button"] == "Refreshing…"
    assert (out["broken"]["button"], out["broken"]["toasts"]) == ("Refresh my picks", 1)
    assert out["out"]["href"] == "/login"


def test_a_session_lost_while_own_picks_are_made_goes_to_login() -> None:
    assert _run("ownLoggedOut") == [["/login", 0, 0]] * 2


def test_own_picks_being_made_when_the_page_loads_are_followed_to_their_end() -> None:
    out = _run("ownRunningAtLoad")
    assert out["made"]["busy"].startswith("Making your picks")
    assert out["made"]["toasts"] == ["Your new picks are in."]
    assert out["none"]["toasts"] == []
    assert out["none"]["after"].startswith("From your plays")
    assert len(out["failed"]["toasts"]) == 1


def test_own_picks_found_being_made_by_a_later_answer_are_followed_to_their_end() -> None:
    out = _run("ownRunningElsewhere")
    assert out["busy"]["button"] == "Refreshing…"
    # The one followed is over, another is running: no word of a failure, and that one is followed.
    assert (out["still"]["button"], out["still"]["toasts"]) == ("Refreshing…", 0)
    assert out["polls"] == 2
    assert (out["after"]["button"], out["toasts"]) == (
        "Refresh in 6 days",
        ["Your new picks are in."],
    )


def test_own_picks_nobody_can_tell_of_any_more_can_be_tried_again() -> None:
    out = _run("ownLost")
    for lost in (out["status"], out["picks"]):
        assert (lost["button"], lost["off"]) == ("Refresh my picks", False)
        assert (lost["text"], lost["toasts"], lost["timers"]) == (
            "From your plays, then new and popular.",
            1,
            0,
        )


def test_a_newer_shared_batch_is_not_told_as_new_own_picks_but_still_shown() -> None:
    out = _run("ownFailedNewerBatch")
    for at, ref in ((out["hadOwn"], "rec-50"), (out["hadNone"], "rec-51")):
        assert at["refs"] == [ref]
        assert at["text"].startswith("Couldn’t make new ones")
        assert at["toasts"] == ["Couldn’t make new picks for you. Try again."]


def test_a_failure_that_cannot_be_tried_again_does_not_say_to() -> None:
    out = _run("ownFailedLast")
    assert out["limit"]["text"] == "That’s all the refreshes for now."
    assert out["limit"]["off"] is True
    assert out["few"]["text"] == "Play five titles and these become yours."
    assert out["noKey"]["text"] == "Couldn’t make new ones."
    assert {t for at in out.values() for t in at["toasts"]} == {"Couldn’t make new picks for you."}


def test_the_day_s_limit_is_no_longer_told_once_its_wait_is_over() -> None:
    out = _run("ownLimitPasses")
    assert (out["before"]["text"], out["before"]["off"]) == (
        "That’s all the refreshes for now.",
        True,
    )
    assert out["after"] == {
        "text": "From your plays, then new and popular.",
        "button": "Refresh my picks",
        "off": False,
    }
    assert out["posts"] == 1


def test_on_a_phone_refresh_says_its_state_in_fewer_words_and_keeps_its_width() -> None:
    assert _run("ownNarrow") == {
        "ready": "My picks",
        "wait": "In 6\u00a0days",
        "busy": "Making…",
    }
    css = (STATIC / "css" / "_watch_browse.css").read_text(encoding="utf-8")
    assert ".row-tool__face[data-narrow]::after { content: attr(data-narrow)" in css
    assert re.search(r"\.row-tool--refresh::before \{ content: attr\(data-widest\)", css)


def test_the_own_refresh_stands_before_the_text_that_changes_from_700px() -> None:
    css = (STATIC / "css" / "_watch_browse.css").read_text(encoding="utf-8")
    wide = re.search(r"@media \(min-width: 700px\) \{([^\n]*(?:\n  [^\n]*)*)", css)
    assert wide and ".rec-line__text { order: 1; }" in wide[1]
    assert "margin-left: 0" in wide[1]


def test_a_pick_has_its_icon_beside_the_words_of_its_caption_at_every_width() -> None:
    css = (STATIC / "css" / "_watch_browse.css").read_text(encoding="utf-8")
    hidden = [
        head
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if "display: none" in body and "watch-card__ai" in head
    ]
    assert hidden == []


def test_the_narrowest_phones_keep_the_icon_of_the_ai_mark() -> None:
    css = (STATIC / "css" / "_watch_browse.css").read_text(encoding="utf-8")
    narrow = "".join(re.findall(r"@media \(max-width: 359\.98px\) \{((?:[^{}]*\{[^{}]*\})*)", css))
    assert ".rec-line > .ai-label" in narrow
    assert ".ico" not in narrow


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
    # rec-10 is rec-7 again and rec-13 rec-9; the batch between them is gone.
    assert out == {"refs": ["rec-7", "rec-8", "rec-9", "rec-12"], "more": False}


def test_a_row_whose_first_batch_is_gone_starts_at_the_older_one_it_goes_on_to() -> None:
    assert _run("goneStart") == ["rec-7", "rec-8", "rec-9", "rec-10"]


def test_the_date_on_the_picks_button_is_in_one_box_with_its_name() -> None:
    # Two boxes would take the button's gap between them, after the dot alone.
    assert _run("picksPill") == ["picks-name", "ico row-tool__chev"]


def test_a_row_whose_older_batches_are_gone_ends() -> None:
    out = _run("goneBatches")
    assert out == {"refs": ["rec-7", "rec-8"], "asked": 4, "more": False}


def test_a_gone_batch_is_not_asked_for_twice_when_the_row_has_no_start() -> None:
    assert _run("goneWithoutStart") == ["GET /api/watch/recommended?batch=2026-11-14"]


def test_picks_of_one_name_kind_and_year_are_told_apart_by_their_tmdb_id() -> None:
    assert _run("picksOfOneName") == ["pick-1", "rec-1", "rec-3", "rec-6"]


def test_an_older_batch_on_its_way_when_the_row_starts_again_is_not_added() -> None:
    assert _run("refreshedWhileLoading") == {"old": ["rec-7", "rec-8"], "now": ["pick-30"]}


def test_focus_moves_into_the_row_that_try_again_brought_back() -> None:
    assert _run("focusAfterRetry") == {"discover": "popular_tv", "recommended": "recommended"}


def test_a_season_line_goes_by_where_each_pill_stands_however_wide() -> None:
    out = _run("unevenSeasonLine")
    # The fourth pill (500-760) is cut off at 700; the seventh (960-1360) at 1200; the line ends at 1460.
    # Back from there, the fourth fits whole in a line that starts at the second (100).
    assert out["arrows"] == [500, 760, 760, 100, 0]
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


def test_a_pick_has_its_more_button_whether_the_library_holds_it_or_not() -> None:
    caption = ["watch-card__lead", "watch-card__tail"]
    assert _run("pickMore") == [
        [None, ["More for Title rec-1"], caption],
        ["movie/11", ["More for Title pick-2"], caption],
        [None, [], []],
    ]


def test_the_grid_of_the_picks_dates_a_pick_back_in_its_row_as_the_others() -> None:
    out = _run("pickBackInGrid")
    latest = out["dates"][0]
    assert (out["group"], out["same"]) == ([latest, latest], True)
    assert out["places"] == [[latest, "0"], [latest, "1"]]


def test_the_discover_rows_keep_their_scroll_when_they_take_the_skeletons_place() -> None:
    assert _run("rowsMovedIn") == {"before": True, "after": False, "left": 300}


def test_the_page_is_held_before_the_discover_rows_fade_in_and_before_they_settle() -> None:
    assert _run("heldAroundTheSwaps") == [[True, False, True], [True, True, True]]
