"""The Watch page's search bar, its results and the category and person pages, run under
node against a fake DOM."""

from __future__ import annotations

import json
import re
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
    assert late["status"] == (
        "1 in your library, 1 available to download for “orchard”. 2 suggestions"
    )


def test_a_library_group_that_shows_only_some_of_what_was_found_says_of_how_many() -> None:
    out = _run("someOfMany")
    assert out["count"] == "2 of 297 titles"
    assert out["status"].startswith("2 of 297 in your library for")
    # Asked again, it says so until the answer is in.
    assert out["again"] == "2 of 297 titles"
    # Of a category's own titles, one that matches by name was counted already.
    assert out["category"] == "3 of 298 titles"


def test_a_reply_to_an_older_or_shorter_query_is_dropped() -> None:
    out = _run("staleReply")
    assert out["afterOlder"] == ["New"]
    assert out["shortCalls"] == ["/api/watch/search?q=a", "/api/watch/search/tmdb?q=a"]
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


def test_a_title_of_one_letter_is_searched_for_outside_the_library_too() -> None:
    out = _run("short")
    assert out["urls"] == ["/api/watch/search?q=M", "/api/watch/search/tmdb?q=M"]
    assert out["titles"] == ["M"]


def test_clearing_returns_to_browse_keeps_focus_and_drops_q_from_the_address() -> None:
    out = _run("clear")
    assert out["withQ"] == "/watch?foo=1&q=orchard#title=movie-2"
    assert out["url"] == "/watch?foo=1#title=movie-2"
    assert (out["value"], out["focus"], out["browse"], out["results"]) == ("", True, True, True)
    assert (out["status"], out["groups"]) == ("", 0)
    assert out["index"] == 0  # typing and clearing leave nothing for Back to undo


def test_a_search_is_one_entry_back_is_the_rows_and_forward_the_search() -> None:
    out = _run("searchEntry")
    assert out["typed"] == ["/watch?q=o", 2, 0]
    assert out["more"] == ["/watch?q=orchard+road", 2]
    assert out["back"] == ["/watch", "", "browse", 700]
    assert out["forward"] == [
        "/watch?q=orchard+road",
        "orchard road",
        "results",
        "/api/watch/search?q=orchard%20road",
    ]
    assert out["linked"] == ["/watch", 1]
    assert out["restored"] == ["/watch?q=orchard", 1]


def test_a_search_typed_while_the_last_is_still_leaving_is_not_lost() -> None:
    assert _run("emptiedAndTyped") == [
        "/watch?q=lantern",
        "lantern",
        "results",
        2,
        ["orchard", "lantern"],
    ]


def test_what_was_typed_over_an_emptied_search_outlives_the_step_back_to_a_category() -> None:
    assert _run("emptiedAndTypedOnCategory") == ["/watch?q=lantern", "lantern", "results", True]


def test_a_shows_leaving_badge_in_a_search_is_worded_for_its_season_and_has_no_keep() -> None:
    rest = ["9", None, 0]
    assert _run("leavingSeason") == [
        ["Season 2 leaves any day", "S2 · Any day", "S2 · Due", "S2 leaves any day", *rest],
        ["Season 2 leaves today", "S2 · Today", "S2 Today", "S2 leaves today", *rest],
        ["Season 2 leaves in 1 day", "S2 · 1 day", "S2 · 1d", "S2 · 1 day left", *rest],
        ["Season 2 leaves in 5 days", "S2 · 5 days", "S2 · 5d", "S2 · 5 days left", *rest],
    ]


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
    assert out["whileLoading"] is True  # nowhere to go until the answer is in
    assert out["tmdbOk"] == "results-group__title|Available to download"
    assert out["tmdbOkInGroup"] is True
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


def test_what_radarr_or_sonarr_has_is_never_under_available_to_download() -> None:
    out = _run("heldByArr")
    assert out["heads"] == ["In your library", "Available to download"]
    assert out["titles"] == ["in_library", "partial", "null"]
    assert out["counts"] == ["2 titles", "1 title"]
    assert out["badge"] == "In library"


def test_a_download_badge_names_its_percentage_and_a_library_one_its_class() -> None:
    out = _run("stateBadges")
    assert out["texts"] == ["Downloading 95%", "Downloading"]
    assert out["said"] == [["Downloading "], []]
    assert out["owned"].endswith("watch-badge--in")


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


def test_what_is_typed_begins_a_word_whatever_its_accents_and_punctuation() -> None:
    assert _run("foldedSuggestions") == [
        "Amé",
        "Ame",
        "Handmaid’s",
        "Spider-Man",
        "Spiderman",
        "WALL·E",
        "Law &",
        "Law and O",
        "B",
        "S.W.A.T",
        "Se7en",
        "…",
        None,
        None,
        None,
        "Smørr",
        "Straße",
        "Xʼs Own",
    ]


def test_a_held_title_that_only_tmdb_found_stands_in_the_library_group() -> None:
    out = _run("heldByTmdb")
    assert out["heads"] == ["In your library", "Available to download"]
    assert out["titles"] == ["Orchard Keepers", "Le Verger", "Orchard Road"]
    assert out["counts"] == ["2 titles", "1 title"]
    assert out["keeps"] == ["7", "2"]
    assert out["status"].startswith("2 in your library, 1 available to download")
    assert out["alone"] == [["In your library"], None, "/watch/movie/2", ["Amélie | Movie"]]
    assert out["again"] == ["Amélie"]
    assert out["many"] == "2 of 81 titles"


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
    assert out["titles"] == ["Orchard Keepers", "Held", "Staying", "Not held"]
    assert out["counts"] == ["3 titles", "1 title"]
    assert out["status"].startswith("3 in your library, 1 available to download")
    assert out["failed"] == [1, None]


def test_a_held_title_of_a_category_phrase_has_its_keep_as_a_match_by_name_has() -> None:
    out = _run("phraseResults")
    leaving = "Leaving in 3 days3 days left"
    assert out["keeps"] == [
        ["7", leaving, "7"],
        ["8", leaving, "8"],
        [None, None, None],
        [None, None, None],
    ]
    assert out["explore"] == [False, "3 days left", "8"]


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
    assert out["inert"] is False


def test_without_tmdb_or_in_the_list_view_the_field_opens_no_sheet() -> None:
    assert _run("noSheet") == {"sheet": False, "suggested": [], "list": False}


def test_a_category_opened_from_a_search_goes_back_to_the_search_and_its_results() -> None:
    out = _run("categoryAndBack")
    assert (out["url"], out["entries"]) == ("/watch?genre=action", 3)
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
    assert out["first"] == [
        genre,
        1,
        ["/api/watch/explore?genre=action&sort=newest&after="],
        "results",
    ]
    assert out["typed"] == ["/watch?q=o", 2, False, True]
    assert out["more"] == ["/watch?q=orchard", 2]
    assert out["back"] == [genre, "", True, False]
    assert out["cleared"] == ["/watch", "browse", True]


def test_a_category_s_place_that_is_no_longer_read_starts_its_list_anew_as_chosen() -> None:
    out = _run("stalePlace")
    assert out["asked"] == "/api/watch/explore?genre=action&sort=newest&decade=2010s&after="
    assert out["url"] == "/watch?genre=action&sort=newest&decade=2010s"
    assert out["years"] == "2010s"
    assert (out["set"], out["next"]) == ([[1, "2,2", True]], "2,2")


def test_a_person_s_title_that_only_radarr_or_sonarr_has_is_in_the_library() -> None:
    assert _run("personHeldByArr") == [
        "In your library2 titles",
        "T1Movie · 2020",
        "T2Movie · 2020",
        "Available to download1 title",
        "T3Movie · 2020",
    ]


def test_films_and_tv_in_one_order_are_asked_to_go_on_from_where_each_stands() -> None:
    assert _run("orderedPages") == [
        ["&sort=newest&after=", "&sort=newest&after=1-5%2C1-9"],
        ["&type=movie&sort=newest", "&type=movie&sort=newest&page=2"],
        ["", "&page=2"],
    ]


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
    # After a failure the grid says what is wrong, without asking once more.
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


def test_a_persons_page_says_what_the_cards_of_a_cast_say() -> None:
    out = _run("personAbout")
    told = {
        "born": "Born 1950, died 2019, aged 69",
        "bio": "A long life.",
        "known": [["movie-1", "Salt"], ["tmdb-movie-9", "Road"]],
        "hidden": [False, False, False],
        "skeletons": 0,
    }
    assert (out["told"], out["again"], out["retried"]) == (told, told, told)
    waiting = out["waiting"]
    assert (waiting["hidden"], waiting["skeletons"] > 0) == ([False, False, False], True)


def test_a_persons_story_opens_only_where_it_is_cut() -> None:
    out = _run("personAbout")
    assert out["more"] == [
        ["More", "false", False, False],
        ["Less", "true", False, True],
        ["More", "false", False, False],
    ]
    assert out["short"] is True


def test_nothing_stands_where_nothing_is_known_of_a_person() -> None:
    out = _run("personAbout")
    for state in ("bare", "failed"):
        assert (out[state]["hidden"], out[state]["skeletons"]) == ([True, True, True], 0)


def test_no_titles_are_counted_of_a_person_who_has_none() -> None:
    assert _run("personBare") == "Actor"


def test_a_persons_story_is_cut_only_once_it_has_closed() -> None:
    assert _run("storyCloses") == [[True, False], [False, True], [True, False], [False, False]]


def test_every_category_has_its_own_drawing_and_no_picture() -> None:
    tiles = _run("categoryTiles")
    assert [t[:3] for t in tiles] == [
        [name, 0, "watch-line"] for name in ("Action", "War", "Made up")
    ]
    assert len({t[3] for t in tiles}) == 3


def test_people_lead_where_no_title_matches() -> None:
    out = _run("peopleLead")
    assert out["person"] == {
        "leads": True,
        "empty": 0,
        "none": ["No title matches “mara”"],
        "kinds": ["person"],
    }
    assert (out["both"]["leads"], out["both"]["none"]) == (False, [])
    assert (out["mixed"]["leads"], out["mixed"]["kinds"]) == (True, ["person", "category"])
    assert (out["category"]["leads"], out["category"]["empty"]) == (False, 1)


def test_people_who_lead_stay_while_more_is_typed() -> None:
    out = _run("peopleLead")
    assert [out[state]["leads"] for state in ("asked", "half", "still", "title", "nobody")] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert out["half"]["none"] == ["No title matches “mara”"]


def test_results_fade_out_over_the_list_that_is_switched_to() -> None:
    assert _run("listOverResults") == [True, True, 0]


def test_a_persons_titles_are_asked_for_again_and_who_they_are_is_not() -> None:
    out = _run("person")
    assert out["kept"] == [2, ["known_for", "person"]]
    assert out["again"] == ["/api/watch/person/9001"]
    assert [t.split("Movie")[0] for t in out["now"]] == ["In your library1 title", "T1"]
    assert out["back"] == "Mara Ellison"


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


def test_esc_and_back_leave_a_category() -> None:
    out = _run("leaveCategory")
    assert out["menus"] == "/watch?genre=action&type=tv"
    assert out["linked"] == ["/watch", 1, "browse", True]
    assert out["opened"] == [["/watch?genre=action", 1], "/watch", 0, "browse"]


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


def test_a_library_result_carries_its_mark_and_eye_beside_its_keep() -> None:
    assert _run("marks") == [["movie/1", True, 1, 1], [None, False, 0, 0]]


def test_a_result_says_when_it_leaves_and_only_by_tomorrow_is_urgent() -> None:
    assert _run("leaving") == [
        [["Leaving any day", "Any day", "Leaving any day"], True],
        [["Leaving today", "Today", "Leaving today"], True],
        [["Leaving in 1 day", "1 day left"], True],
        [["Leaving in 2 days", "2 days left"], False],
    ]


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
    assert out["bare"] == ["Browse by category", "Or try", "More categories"]


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
    out = _run("emptiedStepLost")
    assert (out["url"], out["other"]) == ("/watch?all=movies", True)


def test_a_search_emptied_by_a_step_back_that_is_never_made_leaves_the_search_usable() -> None:
    out = _run("emptiedStepLost")
    assert out["next"] == [["/watch?q=lantern", "results", "/api/watch/search?q=lantern"]] * 2


def test_what_is_typed_before_a_slow_step_back_is_made_stays_and_is_searched_for() -> None:
    searched = ["lantern", "/watch?q=lantern", "results", "/api/watch/search?q=lantern"]
    assert _run("emptiedStepLate") == [searched, searched]


def test_a_back_long_after_a_step_that_was_never_made_closes_the_search() -> None:
    assert _run("emptiedLongAgo") == ["/watch?all=movies", "", "browse"]


def test_a_search_emptied_twice_during_a_slow_step_back_goes_back_once() -> None:
    # The second time before, then after, the step is given up on.
    back = [1, "/watch?all=movies", "", "browse"]
    assert _run("emptiedTwice") == [back, back]


def test_a_search_typed_over_a_category_opened_during_a_lost_step_is_sent() -> None:
    assert _run("emptiedThenCategory") == [
        "/watch?q=lantern",
        "results",
        "/api/watch/search?q=lantern",
    ]


def test_a_place_is_held_for_people_unless_the_library_had_none_the_last_time() -> None:
    # Waiting, then shown: failed, not counted yet, none, some, none, none.
    assert _run("placeHeldForPeople") == [
        [True, False],
        [True, False],
        [False, False],
        [False, True],
        [True, False],
        [False, False],
    ]


def test_the_place_of_late_categories_is_held_and_the_highlight_stays_where_it_was() -> None:
    out = _run("redrawnUnderTheHighlight")
    heads = ["Browse by category", "People in your library", "Or try"]
    assert out["before"] == ["watch-option-try-0", heads, [12, 6, 1]]
    assert out["after"] == ["watch-option-try-0", True, [0, 0, 1]]
    assert out["entered"] == "popular action movies"
    assert out["failed"] == ["watch-sheet__title", "watch-tries"]


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
    assert out["order"] == ["Type", "Order", "Where", "Years"]
    assert out["any"] == [[False, True], [True, True]]
    assert out["library"] == [[False, False], [True, True], True, False]
    assert out["page"] == [[True, True], None, False]
    assert out["failed"] == [False, True]
    assert out["late"] == [True, True]
    assert out["room"] == ["Popular movies and TV", "Popular movies and TV · 10,000 titles"]


def test_tab_goes_on_from_the_field_into_the_rows_behind_the_sheet() -> None:
    # In reach before the key is let through, which is when the browser looks for the next stop.
    assert _run("tabOut") == [[True, False, False, False]] * 2


def test_the_rows_are_gone_though_a_category_opens_while_they_fade() -> None:
    assert _run("pageDuringFade") == ["results", False]


def test_a_search_emptied_over_a_category_or_person_returns_to_their_page() -> None:
    genre = ["/watch?genre=action&sort=newest", 0, 2, True, True]
    assert _run("emptiedOverPage") == [genre, genre, ["/watch?person=9001", *genre[1:]]]


def test_the_cards_of_the_library_alone_do_not_say_they_are_in_the_library() -> None:
    out = _run("where")
    # Not before its own cards are there.
    assert out["held"] == [False, True]
    assert out["heldLinked"] is True


def test_how_many_suggestions_there_are_is_said_at_once_and_with_the_results() -> None:
    assert _run("suggestions")["status"] == "1 in your library for “mar”. 2 suggestions"
    waiting, early, done = _run("lateCategories")["status"]
    assert (waiting, early) == ("", "3 suggestions")
    assert done == "1 available to download for “popular action movies”. 3 suggestions"


def test_the_field_is_a_plain_search_box_in_the_list_view() -> None:
    assert _run("listView")["role"] == [
        [None, None, None, None],
        ["combobox", "list", "false", "watch-sheet"],
    ]


def test_a_word_being_composed_is_searched_for_when_it_is_done_or_rested_on() -> None:
    out = _run("composition")
    assert (out["during"], out["calls"]) == ([400], 0)
    assert out["ended"] == [200]
    assert out["asked"][0].endswith("q=%E6%97%A5")
    assert out["paused"] == 2
    assert out["searched"] == 0
    assert out["page"] == ["/watch?genre=action", False]


def test_the_people_and_what_to_try_stand_before_the_less_used_categories() -> None:
    assert _run("libraryPeople")["bareTiles"] == [
        ["Browse by category", ["action", "science-fiction", "horror"]],
        ["More categories", ["news"]],
    ]


def test_the_page_holds_still_behind_the_sheet_and_the_cross_shuts_it() -> None:
    shut, opened = [False, False, "Clear search"], [True, True, "Close search"]
    # The cross of a search empties it, which opens the sheet again.
    assert _run("sheetHolds") == [shut, opened, [*shut, False], shut, opened]


def test_only_words_that_fit_stand_in_the_empty_field() -> None:
    out = _run("hintFits")
    assert out["list"] == ["Search", "Search your library"]
    label = "Search films and shows"
    # One round of the two examples that are short enough, then it rests.
    assert out["rows"] == [
        label,
        "Try “an actor’s name”",
        "Try “something scary”",
        label,
        label,
    ]


def test_a_category_or_person_refused_for_now_says_so_and_is_asked_for_again() -> None:
    out = _run("tooMany")
    soon = "Too many requests, trying again shortly."
    for page in (out["genre=action"], out["person=9001"]):
        assert page["first"] == [soon, [5000], 1]
        assert page["second"] == [soon, [10000], 2, 1]
        assert page["pressed"] == [[], 3]
        assert page["done"] == [None, [], 1, 0]
        assert page["focus"] == "watch-card__link"  # the button that had it is gone
        assert page["left"] == []
    assert out["ended"] == ["Too many requests. Try again in a while.", []]
    assert out["times"] == 6


def test_a_filter_whose_titles_could_not_be_had_is_asked_for_once() -> None:
    out = _run("tooMany")
    assert out["filter"] == ["Too many requests, trying again shortly.", 2, [5000]]
    assert out["changed"] == []
    assert out["broken"] == ["Couldn’t load the titles.", 3, []]


def test_scrolling_never_takes_the_field_away() -> None:
    out = _run("barStays")
    assert len(out) == 36
    assert all(out)


def test_the_bar_s_elements_stand_in_the_order_they_are_read_at_every_width() -> None:
    out = _run("barOrder")
    wide = ["brand", "search", "watch-sheet", "meter", "profile"]
    assert out["out"] == [
        wide,
        wide,
        # at the foot of a phone, beside the profile button
        ["brand", "meter", "search", "watch-sheet", "profile"],
    ]
    assert (out["back"], out["focused"]) == (wide, True)


def test_the_meter_makes_room_only_where_the_open_field_would_cover_it() -> None:
    out = _run("barMakesRoom")
    assert out["covered"] == [False, True, False]
    for free in ("clear", "narrow"):
        assert out[free] == [False, False, False]
    # Its words came while the field was open.
    assert out["grows"] == [False, False, True, False]


def test_what_the_open_field_covers_comes_back_with_the_focus() -> None:
    out = _run("barGivesBack")
    assert out["wide"] == [[True, True], [False, False], [True, True]]
    # Nothing covers the meter of a phone, nor the profile button of a wide bar.
    assert out["phone"] == [[True, False], [True, False], [True, False]]
    assert out["clear"] == [[True, True], [True, True], [True, True]]


def test_what_the_open_field_covers_can_have_the_focus() -> None:
    sheet = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch_search.css"
    rules = re.findall(r"([^{}]*(?:space-meter\)?|profile-btn)) \{([^}]*)\}", sheet.read_text())
    hidden = [body for _, body in rules if "opacity: 0" in body]
    assert len(hidden) == 3
    for body in hidden:
        assert "pointer-events: none" in body and "visibility" not in body


def test_the_field_stays_between_the_brand_and_the_meter_which_drops_its_words_for_it() -> None:
    out = _run("barBetween")
    assert out["wide"] == [
        ["312px", "878px", False],
        ["522px", "638px", True],
        ["522px", "798px", True],
        [False],
    ]
    assert out["tablet"] == out["wide"]


def test_a_phone_s_meter_drops_a_figure_that_would_be_cut() -> None:
    assert _run("barCutFigure") == [False, True]


def test_slash_keeps_the_page_s_place_unless_the_bar_has_gone_with_it() -> None:
    out = _run("slashKeepsThePlace")
    assert out == {"sticky": [400, True], "gone": [0, True]}


def test_what_the_bar_covers_is_its_one_line_however_far_the_page_is_scrolled() -> None:
    assert _run("barCovers") == [52, 52]


def test_the_field_and_its_sheet_keep_to_what_a_keyboard_leaves_to_be_seen() -> None:
    out = _run("barSeen")
    assert (out["first"]["--seen"], out["first"]["--seen-top"]) == ("844px", "0px")
    assert (out["typing"]["--seen"], out["typing"]["--seen-top"]) == ("500px", "120px")


# ── Ask AI ──

Q = "a gentle heist film"


def test_a_question_is_offered_from_three_words_and_the_search_is_not_held_up_by_it() -> None:
    out = _run("askOffered")
    assert (out["two"], out["less"]) == (None, None)
    assert out["three"].startswith(f"Ask AI: “{Q}”")
    assert out["picked"] is True  # what Return does
    assert out["searched"] == ["/api/watch/search", "/api/watch/search/tmdb"]


def test_without_a_key_nothing_of_the_ai_shows() -> None:
    assert _run("askOffered")["without"] is None
    # The sheet as ever, with the person's own searches before it.
    tonight, heads = _run("askPanel")["off"]
    assert tonight is None
    assert heads == ["Recent", "Browse by category", "Or try", "More categories"]


def test_an_answer_shows_titles_owned_first_and_return_asks_once() -> None:
    out = _run("askAnswered")
    assert out["sent"] == [["POST", "/ask", {"q": Q}]]
    assert out["search"] == 1  # beside the search itself
    assert (out["thinking"].endswith("Looking for titles that fit"), out["busy"]) == (True, "true")
    assert out["head"] == f"“{Q}” | 2 titles, 1 already here"
    assert [t[0] for t in out["titles"]] == ["Nine Locks", "<b>Thunder</b>"]
    assert out["go"][0] == ["Play Nine Locks", "/watch/play/1"]
    assert out["said"] == "2 titles, 1 already here"
    assert out["recorded"] == [["POST", "/searches", {"q": Q}]]
    assert out["asksAgain"] == 1


def test_an_answer_s_words_stay_words_and_its_addresses_stay_on_the_site() -> None:
    out = _run("askAnswered")
    assert out["titles"][1][2] == "<img src=x onerror=alert(1)>"
    assert out["markup"] == [0, 0]
    assert out["links"] == [
        ["movie-1", "/watch/movie/1", 1],
        ["tmdb-movie-2", "#title=tmdb-movie-2", 0],
    ]


def test_a_newer_question_stops_the_one_before_and_only_its_own_answer_shows() -> None:
    out = _run("askNewer")
    assert out["waitsForOld"] == 1
    assert out["stopped"] == [["DELETE", "/runs/7", None]]
    assert out["asked"] == [Q, "a much newer question"]
    assert "Finishing your last question" in out["stopping"]
    assert out["head"].startswith("“a much newer question” | 2 titles")
    assert (out["old"][0].startswith("Ask AI"), out["old"][1]) == (True, 0)


def test_a_question_can_be_stopped_even_before_its_run_is_known() -> None:
    out = _run("askStop")
    assert out["sent"] == [["DELETE", "/runs/7", None]]
    assert (out["head"].startswith("Ask AI"), out["polls"]) == (True, 0)
    assert (out["early"][0].startswith("Ask AI"), out["early"][1]) == (True, ["DELETE"])
    assert out["late"].startswith("Ask AI")


def test_a_question_goes_on_while_the_bar_is_closed_and_is_there_again() -> None:
    out = _run("askSurvives")
    assert out["closed"] == ["", None]
    assert out["still"] is None
    assert out["again"] == [f"“{Q}” | 2 titles, 1 already here", 1]


def test_a_question_left_for_another_page_is_shown_again_but_a_link_asks_nothing() -> None:
    out = _run("askReloaded")
    assert out["mine"] == [Q]
    assert out["recorded"] == 0  # it was when it was asked
    assert out["link"][0] == 0 and out["link"][1].startswith("Ask AI")
    assert out["left"] == ["DELETE/runs/7"]


def test_a_question_of_another_tab_is_stopped_only_by_the_person() -> None:
    out = _run("askReloaded")
    assert out["tab"][0] == 0 and out["tab"][1].startswith("Another question of yours")
    assert out["taken"] == 2


def test_taking_the_place_of_another_tab_s_question_can_be_stopped_and_is_asked_once() -> None:
    out = _run("askTakeover")
    assert out["stop"].startswith("Ask AI")
    assert out["stopped"][0] == [Q] and out["stopped"][1].startswith("Ask AI")
    assert out["twice"] == 2  # as asked the first time, and once more


def test_another_question_asked_meanwhile_is_the_one_that_goes_on() -> None:
    newer = "a much newer question"
    for asked, head, runs in _run("askTakeover")["other"]:
        assert asked == [Q, newer]
        assert head.startswith(f"“{newer}” | Looking")
        assert runs == []  # nor is it stopped


def test_a_question_asked_straight_after_stop_waits_till_the_server_was_told() -> None:
    out = _run("askAfterStop")
    for waits, head, stopped in out["same"] + out["other"]:
        assert waits == [1, 1, 2]
        assert head.endswith("2 titles, 1 already here")
        assert stopped == ["/runs/7"]


def test_a_limit_names_the_number_in_force_and_when_it_ends() -> None:
    out = _run("askRefused")
    assert out["limit"]["head"].startswith("You’ve asked 12 questions today")
    assert "after midnight. Search below works as always." in out["limit"]["head"]
    assert out["one"]["head"].startswith("You’ve asked 1 question today")
    assert not re.search(r"\d", out["noNumber"]["head"])
    assert "40" in out["everyone"]["head"]
    for way in ("limit", "one", "noNumber", "everyone"):
        assert (out[way]["again"], out[way]["offered"]) == (None, True)


def test_a_limit_of_none_and_no_key_read_as_unavailable_and_offer_it_no_more() -> None:
    out = _run("askRefused")
    for way in ("zero", "everyoneZero", "unavailable"):
        assert out[way]["head"].startswith("AI isn’t available right now")
        assert (out[way]["again"], out[way]["offered"]) == (None, False)


def test_what_may_work_the_next_time_can_be_tried_again() -> None:
    out = _run("askRefused")
    for way in ("busy", "quick", "tmdb"):
        assert out[way]["again"] == "Try again"
    assert out["failed"].startswith("The answer didn’t arrive")
    assert out["retried"] == 2


def test_an_answer_of_nothing_offers_simpler_questions() -> None:
    out = _run("askNothing")
    assert out["head"].startswith("No title fits")
    assert Q in out["note"]
    assert out["asked"] == ["a western musical film", [Q, "a western musical film"]]


def test_a_chip_narrows_the_answer_and_pressed_again_gives_all_of_it_back() -> None:
    out = _run("askNarrow")
    assert out["sent"] == {"q": Q, "refine": "Funnier"}
    # What is there stays till the narrower answer is in.
    assert out["narrowing"] == ["Narrowing", ["Nine Locks", "<b>Thunder</b>"], ["Funnier"]]
    assert out["narrowed"] == ["1 title, none already here", ["<b>Thunder</b>"], ["Funnier"]]
    whole = ["2 titles, 1 already here", ["Nine Locks", "<b>Thunder</b>"], []]
    assert out["back"] == [whole, 2]
    assert out["refused"] == [whole, "You’ve asked 5 questions today"]
    assert out["none"][1] == "Nothing here fits “Only in your library”"


def test_a_film_of_an_answer_is_downloaded_once_and_says_so() -> None:
    out = _run("askDownload")
    assert out["read"] == ["/api/watch/title/tmdb-movie-2"]
    assert out["posted"] == [{"tmdb_id": 2}]
    assert out["queued"] == ["Play Nine Locks", "<b>Thunder</b>: Queued"]


def test_the_sheet_opens_on_tonight_the_recent_searches_and_the_person_s_own_phrases() -> None:
    out = _run("askPanel")
    assert out["first"] == "tn"
    assert out["titles"][:2] == ["Recent", "Or try"]
    assert out["tries"] == [
        ["heist", "option", False],
        ["tessera", "option", False],
        ["a heist that goes wrong, gently", "option", True],
        ["more by Ilse", "option", True],
    ]
    # A phrase of one's own is asked; a search of before is searched for.
    assert out["asked"][0] == out["asked"][1][0] == "a heist that goes wrong, gently"
    assert out["recent"] == ["heist", 0]
    assert out["stranger"] == [
        ["popular action movies", False],
        ["popular comedy movies", False],
        ["top rated sci-fi", False],
    ]


def test_clearing_takes_the_searches_and_the_phrases_made_from_them() -> None:
    out = _run("askPanel")
    assert out["notCleared"] == 2  # the server did not
    assert out["cleared"] == ["popular action movies", "popular comedy movies", "top rated sci-fi"]
    assert out["clearedTitle"] == "Try"


def test_the_ai_known_late_moves_nothing_in_an_open_sheet() -> None:
    assert _run("askLate") == {"open": True, "next": True}


def test_a_search_is_kept_when_it_is_meant_and_not_at_every_key() -> None:
    out = _run("askRecorded")
    assert out["typed"] == []
    assert out["entered"] == ["orchard"]
    assert out["opened"] == ["orchard", "salt"]
    assert out["recent"] == ["salt", "orchard", "heist", "tessera"]
    assert out["off"] == 0  # the person's history is not used, or nothing can use it


def test_tonight_shows_what_fits_the_time_pressed_last() -> None:
    out = _run("tonight")
    assert out["first"] == "watch-option-tonight-0"
    assert out["times"] == ["option,false", "option,false", "option,true", "option,false"]
    assert out["sent"] == ["/tonight?minutes=60", "/tonight?minutes=120"]
    assert out["cards"] == [
        ["Film 1", "/watch/movie/1", "<i>why</i>"],
        ["Film 2", "/watch", "<i>why</i>"],
        ["Film 3", "/watch/movie/3", "<i>why</i>"],
    ]
    assert out["sub"][:2] == ["2020 · Drama, ends T", "TV · season 1, ends T"]
    assert out["opened"] == [["movie-1"], True]


def test_tonight_has_others_and_says_when_little_or_nothing_fits() -> None:
    out = _run("tonight")
    assert out["foot"] == ["All in your library, none seen", "The others"]
    assert out["more"] == "/tonight?minutes=120&skip=3"
    assert out["back"][1] == "Back to the first"
    assert out["few"] == ["Only 1 unseen title fits in an hour and a half", None]
    assert "The shortest is Film 4, 1 h 28 min." in out["none"][0]
    assert out["way"] == "/tonight?minutes=90"
    assert out["failed"].startswith("Couldn’t look just now")


def test_titles_like_one_have_a_page_that_back_leaves() -> None:
    out = _run("like")
    assert out["can"] == [True, True, False, False]
    assert out["none"] is False
    assert out["sent"] == [{"ref": "movie-1"}]
    assert out["page"][:2] == ["/watch?like=movie-1", "Like Nine Locks"]
    assert out["answer"] == "More like Nine Locks | 2 titles, 1 already here"
    assert out["back"] == ["/watch", "browse", True]


def test_a_link_to_titles_like_one_asks_nothing_but_the_way_back_to_them_does() -> None:
    out = _run("like")
    assert out["linked"] == [0, "/watch", "browse"]
    assert out["again"][0] == ["movie-1"]
    assert out["again"][1:] == [
        "More like Nine Locks | Looking for titles that fit",
        "Like Nine Locks",
    ]


def test_what_is_remembered_is_the_person_s_own_and_goes_at_sign_out() -> None:
    out = _run("askNextPerson")
    # Nothing is asked, kept or stopped for the next person of the tab.
    assert (out["asked"], out["recorded"], out["runs"]) == (0, 0, 0)
    assert out["head"].startswith("Ask AI")
    assert out["like"] == [0, "/watch"]
    assert out["kept"] is False
    assert out["signedOut"] == ["other"]


def test_where_no_title_has_the_words_return_is_still_said_to_ask() -> None:
    out = _run("askNoTitle")
    assert out["status"] == f"No matches for “{Q}”. Press Return to ask AI"
    assert out["asked"] == f"No matches for “{Q}”"


def test_a_download_from_an_answer_keeps_the_focus_on_its_button() -> None:
    out = _run("askFocus")
    assert out == {"label": "<b>Thunder</b>: Queued", "there": True, "busy": None}


def test_clearing_forgets_the_answers_and_the_question_remembered() -> None:
    out = _run("askForgotten")
    assert out["head"].startswith("Ask AI")
    assert (out["kept"], out["asked"]) == (False, 0)


def test_a_page_back_brings_is_brought_up_to_date() -> None:
    out = _run("askRestored")
    assert out["head"].startswith("Ask AI")
    assert out["tries"] == ["popular action movies", "popular comedy movies", "top rated sci-fi"]
    assert out["other"] == [["/watch"], None]
    assert out["first"].endswith("already here")


def test_titles_like_one_can_be_found_from_the_list() -> None:
    out = _run("askListView")
    assert out["can"] is True
    assert out["page"][0] == "/watch?like=movie-1"
    assert out["page"][1].startswith("More like Nine Locks")


def test_what_is_read_out_stands_outside_what_is_busy() -> None:
    out = _run("askStatus")
    assert out["text"] == ["Looking for titles that fit"]
    assert out["busy"] == [False]


def test_a_page_of_someone_else_s_is_left_when_it_is_looked_at_again() -> None:
    out = _run("askOtherTab")
    assert out["hidden"] == []
    for way in ("visible", "focused", "opened"):
        # With none of their words in the address, the bar or what the tab remembers.
        assert out[way] == [["/watch"], "", False], way
    assert out["sheet"] == 0
    assert out["same"] == []


def test_what_is_sent_says_whose_page_it_is_of_and_a_refusal_leaves_it() -> None:
    out = _run("askOtherTab")
    for way, urls in (
        ("record", ["/searches"]),
        ("ask", ["/searches", "/ask"]),
        ("like", ["/like"]),
    ):
        sent, left = out[way]
        assert [url for _, url, _ in sent] == urls
        assert all(headers == {"X-Assist-Account": "a1"} for _, _, headers in sent), way
        assert left == [["/watch"], "", False], way


def test_words_that_came_with_the_address_are_not_kept_for_a_press_on_a_result() -> None:
    out = _run("askArrived")
    assert out["arrived"] == []
    assert out["typed"] == ["salt"]
    assert out["entered"] == ["orchard"]
