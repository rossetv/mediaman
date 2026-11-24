"""The Watch page's search bar, its results and the category and person pages, run under
node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_search_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_library_group_shows_before_tmdb_answers_and_tmdb_fills_its_own_place() -> None:
    out = _run("libraryFirst")
    assert out["debounce"] == 200
    assert out["urls"] == ["/api/watch/search?q=orchard", "/api/watch/search/tmdb?q=orchard"]
    early = out["afterLibrary"]
    assert early["heads"] == ["In your library", "Available to download"]
    assert early["libraryRefs"] == ["movie-1"]
    assert early["skeleton"] is True
    assert early["shown"] is True
    assert early["keepButtons"] == 1
    late = out["afterTmdb"]
    assert late["heads"] == ["In your library", "Available to download"]
    assert late["counts"] == ["1 title", "1 title"]
    assert late["skeleton"] is False
    assert late["href"] == "#title=tmdb-movie-9"
    assert late["badge"] is True
    assert late["sub"] == "Movie · 2020"
    assert late["status"] == "1 in your library, 1 available to download for “orchard”"


def test_a_reply_to_an_older_or_shorter_query_is_dropped() -> None:
    out = _run("staleReply")
    assert out["afterOlder"] == ["New"]
    assert out["shortCalls"] == ["/api/watch/search?q=a"]
    assert out["afterShort"] == {"titles": ["Short"], "heads": ["In your library"]}


def test_a_failed_tmdb_group_keeps_the_library_and_retries_alone() -> None:
    out = _run("tmdbFailed")
    assert out["heads"] == ["In your library", "Available to download"]
    assert out["libraryCards"] == ["Orchard Keepers"]
    assert out["alert"] == 1
    assert out["retryUrls"] == ["/api/watch/search/tmdb?q=orchard"]
    assert out["retrying"] is True
    assert out["after"]["titles"] == ["Orchard Keepers", "Orchard Road"]


def test_nothing_found_shows_only_when_both_groups_are_empty() -> None:
    out = _run("nothing")
    assert out["whileWaiting"] is None
    assert out["help"] is True
    assert out["line"] == "No matches for “zzqx”"
    assert out["status"] == "No matches for “zzqx”"
    assert out["tmdbOnly"] == {"line": None, "heads": ["Available to download"]}


def test_without_tmdb_the_hint_links_for_admins_and_never_says_tmdb_to_viewers() -> None:
    out = _run("notConfigured")
    assert out["admin"]["keep"] == 1
    assert out["viewer"]["keep"] == 1
    assert out["admin"]["calls"] == 1
    assert out["admin"]["href"] == "/settings#setg-integrations"
    assert "Connect TMDB" in out["admin"]["text"]
    assert out["viewer"]["calls"] == 1
    assert out["viewer"]["href"] is None
    assert "tmdb" not in out["viewer"]["text"].lower()
    assert out["empty"]["admin"]["line"] == "No matches for “zzqx”"
    assert out["empty"]["admin"]["href"] == "/settings#setg-integrations"
    assert "Connect TMDB" in out["empty"]["admin"]["text"]
    assert out["empty"]["viewer"]["href"] is None
    assert "tmdb" not in out["empty"]["viewer"]["text"].lower()
    assert "library" in out["empty"]["viewer"]["text"]
    assert out["fromReply"] is True


def test_a_one_letter_query_does_not_ask_tmdb_but_two_letters_do() -> None:
    out = _run("short")
    assert out["one"] == ["/api/watch/search?q=a"]
    assert out["two"] == ["/api/watch/search?q=ab", "/api/watch/search/tmdb?q=ab"]


def test_clearing_returns_to_browse_keeps_focus_and_drops_q_from_the_address() -> None:
    out = _run("clear")
    assert out["withQ"] == "/watch?foo=1&q=orchard#title=movie-2"
    assert out["url"] == "/watch?foo=1#title=movie-2"
    assert (out["value"], out["focus"], out["browse"], out["results"]) == ("", True, True, True)
    assert (out["status"], out["groups"]) == ("", 0)
    assert out["length"] == 1  # typing and clearing add nothing for Back to undo


def test_in_the_list_view_the_field_announces_the_query_and_runs_no_search() -> None:
    out = _run("listView")
    assert out["delay"] == 350
    assert out["events"] == [{"type": "mm:watch-list-search", "q": "lantern"}]
    assert (out["calls"], out["resultsHidden"]) == (0, True)
    assert out["cleared"] == {"type": "mm:watch-list-search", "q": ""}
    assert out["afterSwitch"]["calls"] == 2
    assert out["afterSwitch"]["placeholder"] == "Search films and shows"


def test_search_cards_put_the_title_ref_on_the_link_so_focus_can_return_to_it() -> None:
    out = _run("cardLink")
    assert out["refs"] == ["movie-1", "tmdb-movie-9"]
    assert out["tags"] == ["A", "A"]
    assert out["onArticle"] == 0


def test_after_a_retry_focus_goes_to_the_rebuilt_group() -> None:
    out = _run("retryFocus")
    assert out["tmdbOk"] == "results-group__title|Available to download"
    assert out["headingFocusable"] == -1
    assert out["failedAgain"] is True
    assert out["library"] == "results-group__title|In your library"


def test_both_searches_failing_is_one_line_with_one_try_again_that_runs_both() -> None:
    out = _run("bothFailed")
    assert out["text"] == "Couldn’t search just now.Try again"
    assert (out["alerts"], out["groups"], out["buttons"]) == (1, 0, 1)
    assert out["status"] == "Couldn’t search just now."
    assert out["retryUrls"] == ["/api/watch/search?q=orchard", "/api/watch/search/tmdb?q=orchard"]
    assert out["focus"] is True


def test_an_empty_library_with_tmdb_down_says_so_in_the_status_and_above_the_error() -> None:
    out = _run("libraryEmptyTmdbFailed")
    assert out["waiting"] is True
    assert out["status"] == "No matches in your library for “zephyrine”"
    assert out["line"] == "Nothing in your library matches"
    assert out["above"] is True
    assert out["heads"] == ["Available to download"]
    assert out["alerts"] == 1


def test_the_results_are_there_at_once_while_the_rows_leave_and_clearing_brings_them_back() -> None:
    out = _run("viewFade")
    assert out["during"] == ["browse,results", True]
    assert out["after"] == ["results", False]
    assert out["back"] == ["browse", False]
    # Cleared half-way: the timer that would have hidden the rows is off.
    assert out["quick"] == ["browse", False]
    assert out["left"] == 0
    assert out["reduced"] == ["results", False]


def test_a_new_search_while_the_last_results_leave_ends_in_one_clean_view() -> None:
    out = _run("settledAfterFade")
    clean = {"skeleton": 0, "pages": 1, "leaving": 0}
    assert out["nothing"] == {"heads": [], "titles": [], "line": "No matches for “zzqx”", **clean}
    # What is leaving is a picture: nothing finds a title or a Keep button in it.
    assert out["midway"] is False
    assert out["chained"] == {
        "heads": ["In your library"],
        "titles": ["Harrow"],
        "line": None,
        **clean,
    }


def test_enter_searches_at_once_and_on_a_phone_puts_the_keyboard_away() -> None:
    out = _run("enter")
    for where in ("phone", "desk"):
        assert out[where]["urls"] == [
            "/api/watch/search?q=orchard",
            "/api/watch/search/tmdb?q=orchard",
        ]
        assert out[where]["prevented"] is True
    assert (out["phone"]["focus"], out["desk"]["focus"]) == (False, True)


def test_a_search_in_the_address_opens_on_its_results_with_the_rows_never_shown() -> None:
    out = _run("openedOnResults")
    assert out["urls"] == ["/api/watch/search?q=orchard", "/api/watch/search/tmdb?q=orchard"]
    assert out["before"] == {"results": False, "browse": True, "skeleton": True}
    assert out["clear"] is True
    assert out["after"] == {"results": False, "browse": True, "titles": ["Orchard Keepers"]}
    assert out["cleared"] == {"results": True, "browse": False}
    assert out["none"] == {"skeleton": 0, "line": "No matches for “zzqx”"}


def test_a_download_badge_names_its_percentage_and_a_part_owned_title_is_in_the_library() -> None:
    out = _run("stateBadges")
    assert out["texts"] == ["Downloading 95%", "Downloading", "In library"]
    assert out["said"] == [["Downloading "], [], []]
    assert out["classes"][2].endswith("watch-badge--in")


def test_a_poster_both_searches_found_stays_and_one_whose_state_changed_is_drawn_anew() -> None:
    out = _run("kept")
    assert out["waiting"] is True
    assert out["dimmed"] == "true"
    # TMDB has not answered yet: what it said before and still fits is not taken away.
    assert out["early"] == [["Orchard Rise", "Orchard Keepers", "Orchard Road"], True]
    assert out["same"] is True
    assert out["redrawn"] == "Downloading 4%"
    assert out["titles"] == ["Orchard Rise", "Orchard Keepers", "Orchard Road"]


def test_categories_are_suggested_on_the_keystroke_and_people_once_they_are_known() -> None:
    out = _run("suggestions")
    assert out["atOnce"] == [
        "Action | Category · Popular",
        "Action | Category · Top rated",
        "Action | Category · Newest",
    ]
    assert out["calls"] == 0
    assert out["expanded"] == "true"
    assert out["named"] == ["Mara Ellison | Actor · Salt", "Marrow | Movie"]
    assert out["known"] == ["Mara Ellison | Actor · Salt"]
    assert out["phrase"][0] == "Action movies | Category · Top rated"
    assert out["alias"][0] == "Horror | Category · Popular"
    assert out["begun"] == "Action | Category · Popular"


def test_the_column_beside_the_grid_goes_only_once_it_is_known_to_stay_empty() -> None:
    assert _run("column") == {
        "first": False,
        "typing": [False, 0],
        "early": False,
        "settled": True,
    }


def test_a_phrase_that_names_a_category_lists_its_titles_each_once() -> None:
    out = _run("phraseResults")
    assert out["urls"][2] == "/api/watch/explore?genre=action&type=movie"
    assert out["waiting"] is True
    assert out["titles"] == ["Orchard Keepers", "Held", "Not held"]
    assert out["counts"] == ["2 titles", "1 title"]
    assert out["status"].startswith("2 in your library, 1 available to download")
    assert out["failed"] == [1, None]


def test_arrows_move_one_highlight_esc_undoes_a_step_at_a_time_and_enter_opens_it() -> None:
    out = _run("keys")
    assert out["open"] == [True, False, True, "true"]
    assert out["tile"] == [["watch-option-genre-action=true"], "watch-option-genre-action"]
    assert out["dropped"] == [[], True]
    assert out["closed"] == [False, False, False]
    assert out["script"] is False
    assert out["row"] == ["watch-option-c-action=true"]
    assert out["none"] == []
    assert out["cleared"] == ["", "browse", True]
    assert out["opened"] == "/watch?genre=action"


def test_slash_goes_to_the_field_but_not_from_a_field_or_behind_a_dialog() -> None:
    out = _run("slash")
    assert (out["plain"], out["sheet"]) == (True, True)
    assert (out["modified"], out["field"], out["dialog"]) == (False, False, False)


def test_without_tmdb_or_in_the_list_view_the_field_opens_no_sheet() -> None:
    assert _run("noSheet") == {"sheet": False, "suggested": [], "list": False}


def test_a_category_opened_from_a_search_goes_back_to_the_search_and_its_results() -> None:
    out = _run("categoryAndBack")
    assert (out["url"], out["entries"]) == ("/watch?genre=action", 2)
    assert (out["value"], out["token"], out["title"]) == ("", "Action", "Action")
    assert out["shown"] == "results"
    assert out["cards"] == []  # nothing of the search is left beside it
    # Its results stand again before the server has answered.
    assert out["back"] == {
        "url": "/watch?q=action",
        "value": "action",
        "token": None,
        "titles": ["Orchard Keepers", "Orchard Road"],
        "explore": False,
        "stopped": True,
    }


def test_typing_on_a_category_is_an_entry_of_its_own_and_the_cross_goes_to_the_rows() -> None:
    out = _run("typeOnCategory")
    genre = "/watch?genre=action&sort=newest"
    assert out["first"] == [genre, 1, ["/api/watch/explore?genre=action&sort=newest"], "results"]
    assert out["typed"] == ["/watch?q=o", 2, False, True]
    assert out["more"] == ["/watch?q=orchard", 2]
    assert out["back"] == [genre, "", True, False]
    assert out["cleared"] == ["/watch", "browse", True]


def test_the_filters_change_the_address_in_place_and_the_last_one_pressed_wins() -> None:
    out = _run("filters")
    assert out["before"] == "Popular movies and TV"
    assert out["loaded"] == ["Popular movies and TV · 1,280 titles", 1, 2]
    assert out["asked"] == [
        "/api/watch/explore?genre=action&type=movie",
        "/api/watch/explore?genre=action&type=movie&sort=top_rated",
    ]
    assert out["address"] == ["/watch?genre=action&type=movie&sort=top_rated", 1]
    assert out["set"][0] == [1, 2, True]
    assert out["said"] == "Top rated movies · 7 titles"  # not the late answer's 9
    assert out["pressed"] == ["Movies", "Top rated", "Anywhere"]
    # After a failure the grid asks for that page itself, and says what is wrong.
    assert out["failed"] == [0, 1, False]
    assert out["again"] == 4


def test_a_category_of_films_alone_offers_no_kind_to_choose() -> None:
    out = _run("oneKind")
    assert out["hidden"] is True
    assert out["says"] == "Popular movies"
    assert out["url"] == "/api/watch/explore?genre=horror"
    # A link that asks for a kind it has not lists all it has.
    assert out["linked"] == ["/watch?genre=horror", "/api/watch/explore?genre=horror"]


def test_a_persons_page_lists_what_the_library_holds_first_under_its_own_heading() -> None:
    out = _run("person")
    assert (out["title"], out["token"]) == ("Mara Ellison", "Mara Ellison")
    assert out["letters"] == "ME"  # under the photo, for when it does not load
    assert out["facts"] == "Actor · 3 in your library, 2 more"
    assert out["status"] == "Mara Ellison, 5 titles"
    assert out["photo"] == "/api/watch/img/tmdb/w342/p.jpg"
    assert [t.split("Movie")[0] for t in out["first"]] == ["In your library3 titles", "T1", "T2"]
    # The heading that was printed on the first page is not printed again on the second.
    assert [t.split("Movie")[0] for t in out["second"]] == [
        "T3",
        "Available to download2 titles",
        "T4Queued",
        "T5Queued",
    ]
    assert out["calls"] == 2


def test_a_persons_title_is_listed_once_and_a_page_of_repeats_leads_on_to_the_next() -> None:
    out = _run("personPages")
    assert [t.split("Movie")[0] for t in out["first"]] == [
        "In your library2 titles",
        "T1",
        "Available to download4 titles",
        "T2",
    ]
    # Held, but found after the rest had begun: no heading, and its badge says so.
    assert [t.split("Movie")[0] for t in out["cards"]] == ["T3", "T4In library"]
    assert (out["next"], out["calls"]) == (4, 4)
    assert out["last"] == [0, None]


def test_esc_back_and_the_browse_switch_leave_a_category() -> None:
    out = _run("leaveCategory")
    assert out["menus"] == "/watch?genre=action&type=tv"
    assert out["linked"] == ["/watch", 1, "browse", True]
    assert out["opened"] == [["/watch?genre=action", 1], "/watch", 0, "browse"]
    assert out["away"] == ["/watch", "browse", True]


def test_the_list_opened_over_a_category_shuts_it_and_back_opens_it_again() -> None:
    out = _run("listOverCategory")
    assert out["list"] == [True, True, None]
    assert out["back"] == ["/watch?genre=action", 2, False, "", 0]


def test_what_is_typed_is_read_as_a_category_a_kind_an_order_and_a_decade() -> None:
    assert _run("phrases") == [
        ["action", "action", "movie", "popular", None],
        ["science-fiction", "science-fiction", "all", "popular", None],
        ["science-fiction", "science-fiction", "tv", "popular", None],
        ["horror", "horror", "all", "newest", "1990s"],
        ["action", "action", "movie", "popular", "2010s"],
        ["", None, "tv", "popular", None],
        ["news", None, "all", "newest", None],
        ["horror", "horror", "all", "popular", None],  # horror has no TV of its own
        ["", None, "all", "popular", None],
        ["", None, "all", "popular", None],
    ]


def test_an_enter_held_down_or_ending_a_composition_searches_nothing() -> None:
    assert _run("enterNotAsked") == 0


def test_a_library_result_carries_its_mark_and_more_beside_its_keep() -> None:
    assert _run("marks") == [["movie/1", True, 1, 1], [None, False, 0, 0]]


def test_a_result_that_leaves_today_or_tomorrow_is_marked_urgent_and_a_later_one_is_not() -> None:
    assert _run("leaving") == [True, True, False]


def test_a_search_before_the_categories_are_in_waits_for_them_and_lists_its_own() -> None:
    out = _run("lateCategories")
    assert out["waiting"] == [None, True]
    assert out["asked"] == ["/api/watch/explore?genre=action&type=movie"]
    assert out["suggested"] == "Action movies | Category · Popular"
    assert out["titles"] == ["Held"]


def test_categories_that_come_late_are_suggested_for_what_was_typed_meanwhile() -> None:
    out = _run("lateCategories")
    assert (out["early"], out["typed"] > 0) == ([], True)


def test_a_search_ends_without_the_categories_when_they_cannot_be_had() -> None:
    assert _run("lateCategories")["without"] == [None, False, 1]


def test_a_category_that_is_not_there_says_so_with_a_way_back_and_nothing_to_try_again() -> None:
    out = _run("badLinks")
    genre = out["genre"]
    assert (genre["title"], genre["pill"]) == ("Category not found", "Category")
    assert (genre["stopped"], genre["filters"], genre["back"]) == (True, 0, 1)
    assert "not found" in genre["status"]
    assert out["away"] == ["/watch", "browse"]


def test_a_person_who_is_not_there_is_said_to_be_so() -> None:
    person = _run("badLinks")["person"]
    assert (person["title"], person["stopped"], person["back"]) == ("Person not found", True, 1)
    assert person["outline"] is False


def test_a_person_who_cannot_be_had_just_now_has_a_heading_and_may_be_tried_again() -> None:
    failed = _run("badLinks")["failed"]
    assert failed["title"] != ""
    assert (failed["stopped"], failed["back"], failed["outline"]) == (False, 0, False)
    assert _run("badLinks")["broken"]["title"] == "Action"


def test_a_decade_that_is_not_listed_is_dropped_from_the_link() -> None:
    out = _run("badLinks")
    first = "/api/watch/explore?genre=action"
    assert out["decade"] == ["/watch?genre=action", [first]]
    assert out["late"][0] == "/watch?genre=action"
    assert out["late"][1][-1] == first
    assert out["listed"][0] == "/watch?genre=action&decade=1990s"


def test_the_sheet_closes_an_open_preview() -> None:
    assert _run("sheetOverPreview") == [1, True]


def test_keys_pressed_during_a_composition_are_left_to_it() -> None:
    untouched = [0, [False, False], "act", 0]
    page = "/watch?genre=action"
    assert _run("composing") == [untouched, page, untouched, page]


def test_any_year_stays_chosen_when_the_years_arrive() -> None:
    assert _run("anyYear") == ["", "/watch?genre=action", 4]


def test_the_field_names_a_list_only_while_it_can_be_seen() -> None:
    out = _run("combobox")
    sheet, shut = ["true", "watch-sheet", True], ["false", "watch-sheet", True]
    assert out["sheet"] == sheet
    assert out["suggestions"] == ["true", "watch-suggestions", True]
    assert out["category"] == shut
    assert out["nothing"] == shut
    assert out["titles"] == ["false", "watch-sheet"]
    assert out["wide"] == ["true", "watch-suggestions"]


def test_what_loads_on_a_category_or_person_is_said_after_what_the_page_is() -> None:
    assert _run("spoken") == [
        "Action, Popular movies and TV · 5 titles. 1 loaded.",
        "1 more loaded.",
        "Mara Ellison, 1 title. 1 loaded.",
    ]


def test_a_search_emptied_over_a_grid_returns_to_that_grid() -> None:
    back = ["/watch?all=movies&sort=az", 0, 2, "", True, 120, False]
    assert _run("emptiedOverGrid") == [back, back, back]


def test_a_person_who_could_not_be_had_is_asked_for_again() -> None:
    assert _run("personAgain") == [2, True, "Mara Ellison"]


def test_a_category_is_narrowed_to_the_library_in_the_address() -> None:
    out = _run("where")
    assert out["shown"] is True
    assert out["asked"] == "/api/watch/explore?genre=action&where=library"
    assert out["address"] == ["/watch?genre=action&where=library", 1]
    assert out["said"] == "Popular movies and TV in your library · 12 titles"
    assert out["anywhere"] == ["/api/watch/explore?genre=action", "/watch?genre=action"]


def test_a_link_to_the_librarys_part_of_a_category_opens_on_it() -> None:
    out = _run("where")
    assert out["linked"] == ["/api/watch/explore?genre=action&where=library", ["In library"]]
    assert out["bogus"] == "/api/watch/explore?genre=action"
    assert out["left"] == "/watch?q=o"


def test_the_library_is_not_offered_until_it_is_known_and_a_link_to_it_says_so() -> None:
    out = _run("where")
    assert out["hidden"] == [True, 0]
    assert out["early"] == [True, True]
    assert out["out"] == ["/watch?genre=action", "/api/watch/explore?genre=action", True]


def test_the_people_the_library_has_most_of_stand_in_the_sheet() -> None:
    out = _run("libraryPeople")
    assert out["heads"] == ["Browse by category", "People in your library", "Or try"]
    assert len(out["sheet"]) == 6
    assert (out["first"], out["option"]) == ("P1Person 18 titles", "option")
    assert out["opened"] == ["/watch?person=9001", "Person 1"]
    assert out["bare"] == ["Browse by category", "Or try"]


def test_a_category_suggested_alone_offers_who_is_often_in_it() -> None:
    out = _run("libraryPeople")
    assert out["alone"] == [
        "Action | Category · Popular",
        "Action | Category · Top rated",
        "Action | Category · Newest",
        "Person 1 | In 4 action titles",
        "Person 2 | In 3 action titles",
        "Person 3 | In 2 action titles",
    ]
    assert out["none"] == 3
    assert out["actor"] == ["/watch?person=9001", "Person 1"]


def test_a_search_emptied_over_a_grid_takes_the_focus_on_no_later_back() -> None:
    assert _run("emptiedStepLost") == {"url": "/watch?all=movies", "other": True}


def test_the_highlight_is_dropped_when_the_sheet_under_it_is_drawn_anew() -> None:
    out = _run("redrawnUnderTheHighlight")
    assert out == {
        "before": "watch-option-try-0",
        "after": None,
        "entered": "",
        "next": "watch-option-genre-action",
    }


def test_a_category_s_name_is_not_said_to_match_nothing_while_the_categories_cannot_be_had() -> (
    None
):
    out = _run("categoriesFailed")
    assert out["text"] == out["status"] == "Couldn’t load the categories just now."
    assert out["line"] is None
    assert out["asked"] == "/api/watch/explore?genre=action"
    assert out["titles"] == ["Orchard Road"]
    assert out["person"] == [None, 1]
    assert out["one"] == [None, "No matches for “z”"]


def test_a_category_s_page_shows_nothing_that_would_move_before_it_is_known() -> None:
    out = _run("drawnOnceKnown")
    # What may come or go stands last, so nothing after it moves.
    assert out["order"] == ["Type", "Order", "Years", "Where"]
    assert out["any"] == [[False, True], [True, True]]
    assert out["library"] == [[False, False], [True, True], True, False]
    assert out["page"] == [[True, True], None, False]
    assert out["failed"] == [True, True]
