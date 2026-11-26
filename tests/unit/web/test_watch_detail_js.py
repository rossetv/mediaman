"""The Watch page's title detail overlay, run under node against a fake DOM with the real
template: opening and closing with the address, downloads, seasons, trailer, admin facts
and Share."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_detail_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_play_on_a_tv_stands_beside_play_only_where_there_is_something_to_play_and_a_way() -> None:
    out = _run("playOn")
    assert out["shown"] == {"label": "Play on a TV", "hidden": False, "afterPlay": True}
    assert out["redrawn"] is True
    # Its row is laid out for it, and drawn again with it.
    assert out["wide"] == [True, True, False]
    assert out["noTvs"]["hidden"] is True
    assert out["notHeld"] is None
    assert out["without"] is None


def test_a_card_opens_at_once_with_its_title_and_its_own_address() -> None:
    out = _run("openFromCard")
    assert out["afterModifiers"] == {"hidden": True, "gets": 0}
    loading = out["loading"]
    assert (loading["hidden"], loading["title"], loading["busy"]) == (False, "Meridian", "true")
    assert loading["focus"] is True
    assert loading["locked"] is True
    assert loading["hash"] == "#title=tmdb-movie-77"
    assert loading["history"] == [["push", "/#title=tmdb-movie-77"]]
    assert out["loaded"]["busy"] is None
    assert "2 h 19 min" in out["loaded"]["meta"]
    assert out["loaded"]["primary"]["text"] == "Download"


def test_closing_a_title_opened_from_a_card_goes_back_and_returns_focus_to_the_card() -> None:
    out = _run("backCloses")
    assert out["history"][-1] == ["go", -1]
    assert (out["leaving"], out["focusOnCard"]) == (True, True)
    assert (out["hidden"], out["unlocked"]) == (True, True)


def test_closing_twice_goes_back_once_and_a_double_click_does_not_reach_what_is_under_it() -> None:
    out = _run("doubleClose")
    assert out["backs"] == 1
    assert out["second"] == {"open": False, "prevented": True, "stopped": True}
    # Long after, a double click on a card is the card's own.
    assert out["later"]["open"] is True


def test_a_title_opened_again_with_forward_returns_focus_to_its_card() -> None:
    assert _run("forwardThenEscape") == {"closed": True, "focus": "show-300"}


def test_a_person_takes_the_place_of_what_is_under_the_title() -> None:
    out = _run("peopleAndBack")
    assert out["who"] == [{"person": "9001"}]
    assert out["person"]["at"] == "?person=9001"
    # At once scrollable; gone once it has stepped aside.
    assert (out["aside"], out["unlocked"], out["hidden"]) == (True, True, True)


def test_back_from_a_person_finds_each_title_as_it_was_left() -> None:
    out = _run("peopleAndBack")
    second, first = out["second"], out["first"]
    assert (second["at"], second["title"]) == ("?person=9001#title=show-300", "Brightwater")
    assert out["personAgain"]["at"] == "?person=9001"
    assert out["personAgain"]["open"] is False
    assert (first["at"], first["open"], first["title"]) == (
        "#title=tmdb-movie-77",
        True,
        "Meridian",
    )
    assert (first["scroll"], first["focus"], first["busy"]) == (420, "cast-0", None)
    assert out["closed"] == {"open": False, "at": ""}


def test_back_from_a_title_opened_inside_another_finds_that_one_as_it_was_left() -> None:
    out = _run("titleOnTitle")
    assert out["second"] == {"title": "Brightwater", "scroll": 0}
    assert out["first"] == {"title": "Meridian", "scroll": 420, "hidden": False, "play": None}


def test_a_title_found_again_is_brought_up_to_date() -> None:
    out = _run("peopleAndBack")
    assert out["first"]["overview"] == "As it was."
    assert out["upToDate"]["overview"] == "As it is now."
    assert out["gets"] == ["/api/watch/title/show-300", "/api/watch/title/tmdb-movie-77"]


def test_back_before_a_title_has_stepped_aside_turns_it_round() -> None:
    assert _run("backMidway") == {
        "hidden": False,
        "aside": False,
        "title": "Meridian",
        "cast": "Mara Ellison",
        "locked": True,
    }


def test_a_title_s_genres_lead_to_explore_and_back_to_them() -> None:
    out = _run("genreLinks")
    assert out["meta"].endswith("Drama, Sci-Fi & Fantasy, Noir12A")
    assert out["hrefs"] == ["/?genre=drama", "/?genre=science-fiction"]
    assert out["modified"] == {"prevented": False, "explored": []}
    assert out["explored"] == [{"genre": "science-fiction"}]
    assert out["gone"] is True
    assert (out["focus"], out["at"]) == ("genre-science-fiction", "#title=tmdb-movie-77")


def test_escape_closes_a_person_s_card_before_the_title() -> None:
    shown, card_gone, gone = _run("escapeAndTheCard")
    assert shown == {"card": True, "title": True}
    assert card_gone == {"card": False, "title": True}
    assert gone["title"] is False


def test_a_direct_link_opens_the_title_and_closing_drops_the_hash_without_history() -> None:
    out = _run("directLink")
    assert out["opened"]["hidden"] is False
    assert out["opened"]["gets"][0] == "/api/watch/title/movie-120"
    assert out["history"] == [["replace", "/"]]
    assert out["hash"] == ""


def test_a_title_no_card_opened_gives_the_focus_to_one_of_its_cards_that_shows() -> None:
    assert _run("directLinkFocus") == {"library": "shown", "scanned": "tmdb-movie-77"}


def test_an_old_recommendation_link_opens_the_pick() -> None:
    out = _run("oldRecommendationLink")
    assert out["history"][0] == ["replace", "/#title=rec-5"]
    assert out["gets"][0] == "/api/watch/title/rec-5"
    assert "two slow mysteries" in out["reason"]


def test_a_person_s_own_pick_is_labelled_and_names_what_they_played() -> None:
    out = _run("ownPick")
    assert (out["label"], out["icon"]) == ("AI pick", True)
    assert "A slow mystery by the sea." in out["reason"]
    assert out["because"] == "Because you played Salt and <b>Harbour</b>"


def test_an_own_pick_downloads_at_its_own_address_is_emailed_about_and_has_no_share_link() -> None:
    out = _run("ownPick")
    assert out["posts"] == ["/api/watch/picks/9/download"]
    assert "email" in out["under"]
    assert out["share"] is False


def test_a_pick_is_labelled_even_with_no_reason_to_give() -> None:
    out = _run("pickWithLess")
    assert out["label"] == "AI pick"
    assert out["one"].endswith("Because you played Salt")


def test_a_pick_the_library_holds_says_why_it_is_one_wherever_it_is_opened_from() -> None:
    out = _run("pickInLibrary")
    assert out["held"] is False
    assert out["late"] == {"play": True, "box": "AI pick", "busy": "true"}
    assert "Because you played Salt" in out["then"]["box"] and out["then"]["busy"] is None
    assert out["forward"].startswith("Because you played Salt")
    assert out["reloaded"].startswith("Because you played Salt")
    assert out["refused"] == "AI pick"
    # The same title opened from a card that is not a pick.
    assert out["notAPick"] is None


def test_a_pick_opened_from_its_card_names_its_kind_while_loading() -> None:
    out = _run("recKind")
    assert out["fromCard"] == "TV series"
    assert out["fromColdLink"] == ""
    assert out["loaded"] == "TV series"


def test_every_open_starts_at_the_top_of_the_title() -> None:
    assert _run("opensAtTop") == {"afterClose": 0, "overAnother": 0}


def test_a_tmdb_title_in_the_library_opens_as_the_library_title() -> None:
    out = _run("libraryMatchSwitches")
    assert out["gets"][:2] == ["/api/watch/title/tmdb-movie-77", "/api/watch/title/movie-120"]
    assert out["hash"] == "#title=movie-120"
    assert out["primary"]["text"] == "Play"


def test_escape_closes_the_trailer_first_then_the_overlay() -> None:
    out = _run("escapeOrder")
    playing = out["playing"]
    assert playing["trailer"] is True
    assert playing["src"] == (
        "https://www.youtube-nocookie.com/embed/abcdefghijk?autoplay=1&rel=0&playsinline=1"
        "&enablejsapi=1&origin=https%3A%2F%2Fmediaman.test"
    )
    assert playing["sandbox"].split() == [
        "allow-scripts",
        "allow-same-origin",
        "allow-presentation",
        "allow-popups",
        "allow-popups-to-escape-sandbox",
    ]
    assert playing["allow"] == "autoplay; encrypted-media; fullscreen"
    assert playing["referrer"] == "strict-origin"
    assert out["afterFirst"] == {
        "trailer": False,
        "frame": False,
        "open": True,
        "close": ["Close", None],
        "focus": True,
    }
    assert out["closedSecond"] is True


def test_a_playing_trailer_is_left_from_close_or_from_its_own_button() -> None:
    playing = _run("escapeOrder")["playing"]
    assert (playing["close"], playing["focus"]) == (["Close trailer", "Close trailer"], True)
    assert (playing["faces"], playing["usable"]) == (["Trailer", "Stop trailer"], True)


def test_back_close_and_a_press_outside_take_the_trailer_before_the_title() -> None:
    out = _run("trailerHistory")
    title = "#title=tmdb-movie-77"
    details = {"open": True, "trailer": False, "hash": title}
    gone = {"open": False, "trailer": False, "hash": ""}
    assert out["back"] == details
    assert out["forward"] == {**details, "trailer": True}
    assert out["backTwice"] == gone
    assert out["button"] == {**details, "asked": 1}
    for way in ("close", "closeLink", "outside"):
        assert out[way] == [details, gone], way
    assert out["doubleClick"] == [details, details]
    assert (out["trailerTwice"], out["stopTwice"]) == ({**details, "trailer": True}, details)
    # Loaded again mid-trailer: the title alone, and one Back still leaves it.
    assert (out["reload"], out["reloadBack"]) == (details, gone)


def test_a_trailer_that_ends_gives_way_to_the_artwork() -> None:
    out = _run("trailerEnds")
    to = "https://www.youtube-nocookie.com"
    assert out["ended"]["playing"] is False
    # Asked to report, then paused, so that it is silent while it fades.
    assert out["ended"]["sent"] == [["listening", None, to], ["command", "pauseVideo", to]]
    assert out["ended"]["focus"] == "trailer"
    # A press beside a button can leave the focus on the title, which the trailer hides.
    assert out["endedOnTitle"]["focus"] == "trailer"
    assert (out["endedInfo"]["playing"], out["endedInfo"]["focus"]) == (False, "primary")
    for other in ("paused", "otherOrigin", "otherFrame", "junk"):
        assert out[other]["playing"] is True, other


def test_a_trailer_started_from_further_down_is_brought_into_view() -> None:
    out = _run("trailerInView")
    assert out["moving"] == {"top": 0, "behavior": "smooth"}
    assert out["still"] == {"top": 0, "behavior": "auto"}


def test_the_head_eases_between_the_heights_it_really_has_as_its_trailer_comes_and_goes() -> None:
    out = _run("trailerHeight")
    grow = {"from": "234px", "to": "552px", "ms": 250, "cut": False}
    shrink = {"from": "552px", "to": "234px", "ms": 250, "cut": False}

    assert out["openAndClose"] == [grow, shrink]
    halfway = {**shrink, "from": "400px"}
    assert out["closedHalfway"] == [{**grow, "cut": True}, halfway]
    # The next title's head stands at its own height at once.
    assert all(ease["cut"] for ease in out["anotherTitle"])
    assert out["still"] == []


def test_a_title_without_a_trailer_has_no_trailer_button() -> None:
    assert _run("noTrailerKey") == {"trailer": False, "primary": "Download"}


def test_the_bar_of_a_tv_may_have_the_focus_over_a_title_and_escape_still_closes_the_title() -> (
    None
):
    assert _run("tvBar") == {"bar": "tv-pause", "behind": "ttl-title", "closed": True}


def test_a_title_finds_more_like_itself_beside_play_once_there_is_an_ai_to_ask() -> None:
    assert _run("like") == {
        "before": False,
        "acts": ["primary", "like"],
        "focus": "primary",
        "asked": [
            ["movie-120", "Glass Harbour: Restored", "/api/watch/poster/120"],
            ["tmdb-movie-77", "Meridian", None],
            ["tmdb-movie-77", "Meridian", None],
        ],
        "off": False,
    }


def test_escape_handled_by_an_open_keep_menu_leaves_the_overlay_open() -> None:
    assert _run("keepMenuEscapeFirst")["open"] is True


def test_a_failed_load_says_so_with_try_again_and_close() -> None:
    out = _run("loadFails")
    assert out["failed"] == {"line": "Couldn't load details", "buttons": ["Try again", "Close"]}
    assert out["retried"]["text"] == "Download"


def test_a_title_that_is_gone_offers_no_try_again() -> None:
    assert _run("loadNotFound") == ["Close"]


def test_download_turns_into_queued_then_downloading_then_in_library_in_place() -> None:
    out = _run("downloadMovie")
    assert out["busy"]["text"] == "Adding…"
    assert out["posts"] == [
        {
            "url": "/api/search/download",
            "body": {"media_type": "movie", "tmdb_id": 77, "title": "Meridian"},
        }
    ]
    queued = out["queued"]
    assert "dl-state--queued" in queued["primary"]["cls"]
    assert queued["primary"]["text"] == "Queued"
    assert "Downloads" in queued["note"]
    assert queued["focusKept"] is True
    assert queued["email"] is False
    assert out["downloading"]["primary"]["text"] == "Downloading 42%"
    assert out["downloading"]["bar"] == 42
    assert "3.1 GB of 7.4 GB" in out["downloading"]["line"]
    assert "about 6 min left" in out["downloading"]["line"]
    assert out["downloading"]["note"] == "Follow it in Downloads."
    assert out["downloading"]["to"] == "/downloads"
    assert out["done"]["primary"]["text"] == "In library"
    assert "Added just now" in out["done"]["note"]
    named = {"ref": "tmdb-movie-77", "title": "Meridian", "kind": "movie", "year": 2026}
    assert out["events"] == [
        {**named, "download_state": "queued", "download_pct": None},
        {**named, "download_state": "downloading", "download_pct": 42},
        {**named, "download_state": "in_library", "download_pct": None},
    ]
    assert any("queued" in t for t in out["toasts"])


def test_a_queued_download_the_arr_no_longer_holds_gives_the_download_button_back() -> None:
    out = _run("queuedThenGoneOnReopen")["answered"]
    assert (out["before"], out["after"]) == ("Queued", "Download")
    assert out["events"][-1] == {
        "ref": "tmdb-movie-77",
        "title": "Meridian",
        "kind": "movie",
        "year": 2026,
        "download_state": None,
        "download_pct": None,
    }


def test_a_reply_without_the_arr_s_answer_leaves_a_queued_download_queued() -> None:
    out = _run("queuedThenGoneOnReopen")["unanswered"]
    assert (out["before"], out["after"]) == ("Queued", "Queued")
    assert all(e["download_state"] == "queued" for e in out["events"])


def test_a_poll_answering_unknown_keeps_queued_and_polls_again_later() -> None:
    out = _run("queuedThenUnknownOnPoll")
    assert (out["before"], out["after"]) == ("Queued", "Queued")
    assert all(e["download_state"] == "queued" for e in out["events"])
    assert out["pollsLeft"] == [30000]


def test_a_poll_answering_searching_asks_the_title_again_and_its_answer_clears_it() -> None:
    out = _run("searchingRechecks")
    assert out["firstPoll"] == {"gets": 1, "primary": "Queued"}
    assert out["soonAfter"] == {"gets": 1, "primary": "Queued"}
    assert out["afterMinute"] == {"gets": 2, "primary": "Download"}
    assert out["events"][-1]["download_state"] is None
    assert out["pollsLeft"] == 0


def test_just_after_a_download_an_arr_that_does_not_know_it_yet_leaves_it_queued() -> None:
    out = _run("justDownloadedGrace")
    assert (out["afterPoll"], out["afterReopen"]) == ("Queued", "Queued")
    assert out["afterGrace"] == "Download"


def test_a_title_arriving_mid_download_shows_its_real_percent_at_once() -> None:
    assert _run("arrivesDownloading") == {"primary": "Downloading 63%", "bar": 63}


def test_a_queued_download_that_does_not_move_is_polled_less_often_and_not_after_close() -> None:
    out = _run("pollBacksOff")
    assert out["early"] == [5000]
    assert out["late"] == [30000]
    assert out["pollsAfterClose"] == 0


def test_a_title_the_arr_already_holds_says_in_library_without_added_just_now() -> None:
    out = _run("heldInLibrary")
    assert out["primary"] == "In library"
    assert not any("Added just now" in n for n in out["notes"])


def test_the_loading_view_names_the_kind_of_title() -> None:
    assert _run("loadingKind") == {"movie": "Movie", "show": "TV series"}


def test_a_films_collection_stands_above_its_cast_and_its_place_is_held() -> None:
    out = _run("collectionPlace")
    held = ["ttl__lead", "ttl__collsec", "ttl__castsec"]
    bare = ["ttl__lead", "ttl__castsec"]
    assert out["film"] == {"loading": bare, "drawn": held}
    assert out["lone"] == {"loading": bare, "drawn": bare}
    show = ["ttl__lead", "ttl__seasonsec", "ttl__castsec"]
    assert out["show"] == {"loading": show, "drawn": show}


def test_a_series_shows_no_director() -> None:
    assert _run("showDirector") == {"credit": None}


def test_a_poll_mid_share_keeps_sharing_and_copied_and_sends_one_request() -> None:
    out = _run("shareDuringPoll")
    assert out["during"] == {"label": "Sharing…", "busy": "true", "primary": "Downloading 20%"}
    assert out["shares"] == 1
    assert out["copied"] == "Copied"
    assert out["copiedAfterPoll"] == "Copied"
    assert out["primary"] == "Downloading 30%"


def test_a_failed_download_shows_the_reason_inline_and_try_again_sends_it_again() -> None:
    out = _run("downloadFails")
    assert "Radarr did not answer" in out["err"]["text"]
    assert out["err"]["role"] == "alert"
    assert out["err"]["primary"]["text"] == "Download"
    assert out["posts"] == 2
    assert out["events"] == []


def test_a_download_error_stands_above_the_list_of_seasons() -> None:
    assert _run("downloadErrorPlace") == ["ttl__error", "season-pick"]


def test_a_download_error_goes_when_the_picked_seasons_change_and_keeps_its_space() -> None:
    out = _run("downloadErrorThenPicks")
    gone = {"before": "shown", "after": "held", "focusKept": True, "reopened": "none"}
    assert out["chip"] == {**gone, "download": "Download 1 season"}
    assert out["clear"] == {**gone, "download": "Pick seasons"}
    # Select all with all already picked changes nothing.
    assert out["unchanged"] == {
        "before": "shown",
        "after": "shown",
        "focusKept": True,
        "download": "Download 2 seasons",
        "reopened": "shown",
    }


def test_a_movie_the_arr_already_has_is_busy_until_its_reply_then_shows_what_that_says() -> None:
    out = _run("downloadAlreadyThere")
    for name in ("queued", "downloading", "none", "noneYet", "failed", "inLibrary"):
        run = out[name]
        # Neither an error nor "In library" before the reply answers; nothing told the cards yet.
        assert run["at409"]["primary"]["text"] == "Adding…", name
        assert run["at409"]["primary"]["disabled"] is True, name
        assert (run["at409"]["error"], run["at409"]["retry"]) == (False, False), name
        assert run["eventsAt409"] == 0, name
        assert (run["refetched"]["error"], run["refetched"]["retry"]) == (False, False), name
        assert run["toasts"] == ["'Meridian' has already been added"], name
    assert out["unsaid"] == ["Meridian has already been added."]
    event = {"ref": "tmdb-movie-77", "title": "Meridian", "kind": "movie", "year": 2026}
    queued = out["queued"]
    assert queued["refetched"]["primary"]["text"] == "Queued"
    assert queued["events"] == [{**event, "download_state": "queued", "download_pct": None}]
    assert queued["polls"] == 1
    downloading = out["downloading"]
    assert downloading["refetched"]["primary"]["text"] == "Downloading 40%"
    assert downloading["events"] == [{**event, "download_state": "downloading", "download_pct": 40}]
    # Radarr has it but lists nothing: Download would only 409 again.
    assert out["none"]["refetched"]["primary"]["text"] == "Already added"
    assert out["none"]["refetched"]["primary"]["disabled"] is False
    assert out["none"]["events"] == []
    assert out["failed"]["refetched"]["primary"]["text"] == "Already added"
    assert out["failed"]["refetched"]["primary"]["disabled"] is False
    assert out["failed"]["events"] == []
    # It is asked again, slowly, for as long as it is already there.
    for name in ("none", "noneYet", "failed"):
        assert (out[name]["polls"], out[name]["slowPolls"]) == (0, 1), name
    # Still none once Radarr answers later: it was removed, and can be downloaded.
    assert (out["none"]["later"], out["none"]["pollsLater"]) == ("Download", [0, 0])
    # No answer is no news.
    assert out["noneYet"]["later"] == "Already added"
    assert (out["noneYet"]["laterEvents"], out["noneYet"]["pollsLater"]) == ([], [0, 1])
    # Queued later: the panel and the cards say it, and it is followed.
    assert out["failed"]["later"] == "Queued"
    assert out["failed"]["laterEvents"] == [
        {**event, "download_state": "queued", "download_pct": None}
    ]
    assert out["failed"]["pollsLater"] == [1, 0]
    # In the library: it opens as the library title it now is.
    library = out["inLibrary"]
    assert library["events"] == [{**event, "download_state": "in_library", "download_pct": None}]
    assert library["gets"][-1] == "/api/watch/title/movie-120"
    assert library["refetched"]["primary"]["text"] == "Play"


def test_a_show_sonarr_already_tracks_is_already_in_sonarr_unless_its_reply_says_more() -> None:
    out = _run("downloadShowTracked")
    tracked = out["tracked"]
    assert tracked["at409"] == {"primary": "Already added", "retry": False}
    assert tracked["gets"].count("/api/watch/title/tmdb-tv-90") == 2
    assert (tracked["refetched"], tracked["retry"]) == ("Already added", False)
    assert tracked["events"] == []
    assert (tracked["polls"], tracked["reopened"]) == (0, "Already added")
    # Queued, coming in or all there: the panel and every card say it, once, before any reopening.
    event = {"ref": "tmdb-tv-90", "title": "Hollow Pines", "kind": "show", "year": 2026}
    for name, state, pct, text, polls in (
        ("queued", "queued", None, "Queued", 1),
        ("downloading", "downloading", 40, "Downloading 40%", 1),
        ("allThere", "in_library", None, "In library", 0),
    ):
        run = out[name]
        assert run["at409"]["primary"] == "Already added", name
        assert (run["refetched"], run["reopened"]) == (text, text), name
        assert run["events"] == [{**event, "download_state": state, "download_pct": pct}], name
        assert run["polls"] == polls, name
        assert run["reopenedEvents"] == run["events"], name
    # The title asked for again is in the library: it opens as the library show.
    assert out["inLibrary"]["at409"]["primary"] == "Already added"
    assert out["inLibrary"]["refetched"] == "Resume S2 · E4"
    assert out["inLibrary"]["events"] == [
        {**event, "download_state": "in_library", "download_pct": None}
    ]


def test_already_there_is_dropped_on_reopening_once_the_arr_answers() -> None:
    out = _run("trackedReopened")
    for kind, tracked, download in (
        ("movie", "Already added", "Download"),
        ("show", "Already added", "Download 2 seasons"),
    ):
        assert out[kind]["gone"] == [tracked, download], kind
        assert out[kind]["noAnswer"] == [tracked, tracked], kind
        assert out[kind]["queued"] == [tracked, "Queued"], kind


def test_a_state_learned_while_another_title_is_open_names_its_own_title() -> None:
    out = _run("eventAfterAnotherOpened")
    event = {
        "ref": "rec-5",
        "download_state": "queued",
        "download_pct": None,
        "title": "Meridian",
        "kind": "movie",
        "year": 2026,
    }
    assert out["accepted"] == {"away": [event], "reopened": [event]}
    assert out["refused"] == {"away": [event], "reopened": [event]}


def test_opening_a_title_brings_its_cards_into_line() -> None:
    assert _run("openedTellsCards") == {
        "gone": [["rec-5", None, "Meridian"]],
        "noAnswer": [],
        "inLibrary": [["rec-5", "in_library", "Meridian"]],
    }


def test_a_pick_downloads_through_its_own_route_and_promises_the_email() -> None:
    out = _run("recDownload")
    assert out["posts"] == [{"url": "/api/recommended/5/download"}]
    assert out["email"] is True
    assert out["share"] is False


def test_a_pick_promises_an_email_only_where_its_reply_says_this_person_gets_one() -> None:
    out = _run("recEmail")
    for name in ("movie", "show", "noEmail"):
        run = out[name]
        # Refused as already there, or another person's: none, reopened or coming in.
        assert run["refused"] == {"primary": "Queued", "email": False}, name
        assert run["reopened"] == {"primary": "Queued", "email": False}, name
        assert run["downloading"] == {"primary": "Downloading 40%", "email": False}, name
        assert run["ownDownloading"] == {"primary": "Downloading 40%", "email": True}, name
        # Accepted: nothing until the reply says so.
        assert run["beforeReply"] == {"primary": "Queued", "email": False}, name
        assert run["accepted"] == {"primary": "Queued", "email": name != "noEmail"}, name


def test_season_chips_count_the_picked_seasons_and_send_them() -> None:
    out = _run("seasonPicker")
    assert out["initial"]["chips"] == [
        ["1", "true", None],
        ["2", "true", None],
        ["3", "true", "true"],
    ]
    assert out["initial"]["primary"]["text"] == "Download 2 seasons"
    assert "1 season already in library" in out["initial"]["summary"]
    assert out["cleared"]["primary"] == {
        "text": "Pick seasons",
        "cls": "btn btn--lg btn--primary ttl__download",
        "disabled": True,
    }
    assert out["postsWhenNone"] == 0
    assert out["one"]["primary"]["text"] == "Download 1 season"
    body = out["posts"][0]["body"]
    assert (body["monitored_seasons"], body["search_seasons"]) == ([2, 3], [2])


def test_every_show_lists_its_seasons_with_the_state_of_each() -> None:
    out = _run("seasonStates")
    rows = [
        ["1", "Downloading", False],
        ["2", "Queued", False],
        ["3", "Partly here · get the rest", True],
        ["4", "8 episodes", True],
    ]
    assert out["tracked"] == out["queued"] == out["pick"] == rows
    # Of a show in the library, those not wholly here alone.
    assert out["whole"] == {"list": False, "link": False}
    assert out["held"] == [
        ["3", "8 episodes", True],
        ["4", "Not out yet · asked for", False],
    ]


def test_a_season_that_is_refused_says_why_and_cannot_be_asked_for() -> None:
    refused = _run("seasonStates")["refused"]
    assert [c[1:] for c in refused["chips"]] == [
        ["Leaving soon", False],
        ["Deleted", False],
        ["Can’t be asked for yet", False],
        ["Downloads are off", False],
    ]
    for word in ("Keep", "bring back", "list", "Ask an admin"):
        assert word in refused["why"]
    assert (refused["go"], refused["posts"]) == (True, 0)


def test_seasons_are_asked_for_once_and_only_the_one_that_failed_again() -> None:
    out = _run("seasonsAsked")
    assert out["none"] == {"label": "Pick seasons", "off": "true"}
    assert (out["label"], out["busy"]) == ("Download 3 seasons", "Adding…")
    body = {"media_type": "tv", "tmdb_id": 90, "title": "Hollow Pines"}
    assert out["posts"] == [
        ["/api/search/download", {**body, "search_seasons": [3, 4, 5]}],
        ["/api/search/download", {**body, "search_seasons": [4]}],
    ]
    half = out["half"]
    assert half["chips"] == [
        ["3", "Asked for", False],
        ["4", "8 episodes", True],
        ["5", "Downloads are off", False],
    ]
    assert half["said"].startswith("Added season 3. Couldn’t add season 4.")
    assert (half["error"], half["gets"]) == (True, 2)
    assert (out["said"], out["error"]) == ("Added season 4.", False)


def test_a_refused_season_offers_no_retry_and_one_that_failed_does() -> None:
    out = _run("seasonsRefused")
    assert out["refused"] == {"said": "Season 4 is leaving.", "retry": False}
    taken = out["taken"]
    assert (taken["chip"][1], taken["retry"]) == ("Asked for", True)
    assert "Season 4 is asked for." in taken["said"] and "ouldn" not in taken["said"]
    assert out["unread"] == ["4", "Leaving soon", False]
    assert out["failed"]["retry"] is True
    assert "Sonarr did not answer." in out["failed"]["said"]


def test_what_is_said_of_seasons_asked_for_is_what_is_known() -> None:
    out = _run("seasonsUnsure")
    said = "Season 4 is asked for.Try again"
    assert out["lost"] == {"said": said, "bad": False, "retry": True, "chip": "Asked for"}
    assert (out["unread"]["bad"], out["unread"]["retry"]) == (True, True)
    assert (out["dup"]["bad"], out["dup"]["retry"]) == (False, False)
    noted = out["unrecorded"]
    assert "could not be noted" in noted["said"]
    assert (noted["bad"], noted["retry"], noted["chip"]) == (True, True, "Asked for")
    assert out["first"] == {"primary": "Already added", "error": False}


def test_what_another_request_is_said_to_ask_for_is_said_no_longer_than_it_is_so() -> None:
    out = _run("seasonsUnsure")
    said = "Already being asked for."
    assert out["taken"] == [said, said, said]
    assert out["nothing"] == out["unreached"] == [said, said, ""]


def test_a_download_refused_season_by_season_says_so_of_each() -> None:
    assert _run("seasonsUnsure")["each"] == {
        "said": "Season 1 is leaving.Try again",
        "retry": True,
        "top": None,
        "primary": "Already added",
        "toasts": 0,
        "chip": "Leaving soon",
    }


def test_no_answer_is_told_from_one_that_says_nothing() -> None:
    again = "Try again"
    assert _run("seasonsUnsure")["worded"] == [
        "No answer came." + again,
        "That didn’t work." + again,
        "That didn’t work." + again,
    ]


def test_a_toast_of_seasons_that_partly_failed_is_no_success() -> None:
    out = _run("seasonsUnsure")
    assert (out["half"], out["whole"]) == (None, "success")


def test_without_sonarr_nothing_is_offered_and_nothing_read_again() -> None:
    out = _run("seasonsNoSonarr")
    assert out == {"chips": [["4", "Not out yet", False]], "go": True, "why": "", "timers": 0}


def test_a_reading_older_than_an_asking_changes_nothing() -> None:
    out = _run("seasonsOlderReading")
    assert out["library"] == [["3", "Asked for", False]]
    assert out["marks"] == [[["3", "Asked for", False]], [["3", "8 episodes", True]]]
    assert out["notified"] == ["Asked for", "Asked for"]
    assert out["polled"] == out["refused"] == ["Asked for", "Asked for"]


def test_seasons_whose_states_are_to_come_are_read_again_so_often_and_the_page_stays() -> None:
    out = _run("seasonsPending")
    comes, never = out["comes"], out["never"]
    assert comes["first"] == ["3", "Checking…", False]
    assert (comes["last"], comes["asks"]) == (["3", "8 episodes", True], 3)
    assert (comes["go"], comes["still"]) == ("Pick seasons", True)
    assert never["asks"] == 6
    assert never["press"] == {"label": "Check again", "busy": "Checking…"}
    assert never["go"] == "Check again"  # the reading came to an end


def test_a_check_shows_a_while_goes_on_while_sonarr_is_asked_and_says_what_came_of_it() -> None:
    out = _run("seasonsChecked")
    assert out["unknown"] == {
        "at": "Checking…",
        "waits": True,
        "go": "Check again",
        "said": "Still can’t tell what can be asked for.",
    }
    assert out["back"]["said"] == "1 season can be asked for."
    assert out["failed"] == {"go": "Check again", "said": out["unknown"]["said"]}
    assert out["grown"] == "Pick seasons"
    assert out["between"] == [2, 1, "Adding…"]
    # Not there at once: it is still being asked, and that is read again.
    assert out["pending"]["during"] == ["Checking…", True, 2000]
    assert (out["pending"]["go"], out["pending"]["said"]) == ("Pick seasons", out["back"]["said"])


def test_an_answer_that_is_no_success_is_brought_into_view() -> None:
    out = _run("seasonsRevealed")
    assert out["refused"] == [["ttl__error", "nearest", "smooth"]]
    assert out["still"] == [["ttl__error", "nearest", "auto"]]
    assert out["asked"] == []
    assert out["download"] == [["ttl__error", "nearest", "smooth"]]
    assert out["lost"] == "Couldn't add Hollow Pines: No answer came.Try again"


def test_the_error_keeps_its_room_while_what_failed_is_asked_for_again() -> None:
    assert _run("seasonsRevealed")["again"] == {"lines": 1, "inert": True, "after": ["Added."]}


def test_without_sonarr_a_season_still_to_come_is_not_ticked_as_if_it_were_out() -> None:
    out = _run("seasonsDown")
    assert out["primary"] == "Download 2 seasons"
    assert out["chips"] == [
        ["1", "8 episodes", True],
        ["2", "8 episodes", True],
        ["3", "Out 31 Jan 2999", True],
        ["4", "Not out yet", True],
    ]


def test_the_seasons_are_headed_by_what_they_are() -> None:
    out = _run("seasonsNamed")
    # One that has its pill above is no other season.
    assert out["partly"] == ["Missing episodes", "Missing episodes"]
    assert out["more"] == ["More seasons", "More seasons"]


def test_what_is_to_be_asked_for_says_when_it_comes_and_what_starts_with_it() -> None:
    out = _run("seasonsSummed")
    assert out["both"] == "2 seasons will be downloaded · 1 more when it is out"
    assert out["later"] == "1 season will be downloaded when it is out"
    assert out["starts"] == "1 season will be downloaded · Season 3 and Specials start too"
    assert out["named"] == "2 seasons will be downloaded · Specials start too"
    assert out["none"] == "No seasons picked"
    assert out["on"] == "1 season will be downloaded"


def test_a_season_that_is_refused_shows_the_lock_whatever_is_here_of_it() -> None:
    assert _run("seasonsLocked") == [["none", "i-lock"], ["none", "i-lock"]]


def test_no_season_names_sonarr() -> None:
    out = _run("seasonStates")["refused"]
    assert "Sonarr" not in out["why"] + str(out["chips"])


def test_all_below_the_actions_slides_once_as_the_line_under_them_comes() -> None:
    out = _run("seasonsSlide")
    assert out["moved"]["ttl__overview"] == out["moved"]["season-pick__head"] == [-35]
    assert out["moved"]["season-pick__summary"] == [-35]
    assert out["moved"]["ttl__after"] == [-45]
    assert out["chips"] == [[-35], [-35], [-35]]
    # Out of sight above, what is in view stays.
    assert (out["scrolled"], out["slidThen"]) == (35, 0)


def test_a_season_pushed_to_another_row_eases_in_there_and_crosses_none() -> None:
    # 2 joins 3, 4 and 5, two to a row: 3 and 5 stay in theirs.
    assert _run("seasonsRows") == {"slid": ["3", "5"], "faded": ["2", "4"]}


def test_a_season_that_joins_the_others_takes_the_place_of_none() -> None:
    out = _run("seasonsJoined")
    assert out["numbers"] == ["2", "3", "4"]
    assert out["kept"] == [True, True]
    assert out["focus"] == "3"


def test_a_pick_is_downloaded_whole_or_by_its_picked_seasons() -> None:
    out = _run("pickSeasons")
    assert (out["whole"]["body"], out["whole"]["go"]) == (None, True)
    assert out["part"]["body"] == {"monitored_seasons": [1, 3], "search_seasons": [1, 3]}
    assert out["part"]["primary"] == "Queued"
    # One that is not out is not ticked to begin with; ticked by hand, the show is wanted whole.
    assert out["out"]["body"]["search_seasons"] == [1, 2]
    assert out["withComing"]["body"] is None


def test_a_show_sonarr_tracks_or_fully_held_offers_no_download() -> None:
    out = _run("tvBlocked")
    assert out["tracked"] == {
        "primary": {
            "text": "Already added",
            "cls": "dl-state dl-state--done",
            "disabled": False,
        },
        "button": False,
    }
    assert out["all"]["primary"]["text"] == "All seasons in library"
    assert out["all"]["button"] is False


def test_a_season_pill_swaps_the_episodes_in_place_and_keeps_focus() -> None:
    out = _run("seasonPills")
    assert out["first"]["pills"] == [["Season 1", "false"], ["Season 2", "true"]]
    assert "Two One" in out["first"]["eps"]
    assert "One One" in out["eps"]
    assert out["label"] == "Season 1 episodes"
    assert (out["replaced"], out["pressed"], out["focusOnPill"]) == (True, "true", True)
    assert out["resume"]["text"] == "Resume S2 · E4"
    assert out["progress"] is True
    assert out["keep"] == "3010"


def test_an_episode_says_what_it_is_about_when_it_aired_and_what_was_seen_of_it() -> None:
    watched, begun, other, unaired = _run("seasonTexts")["rows"]
    assert (watched["about"], watched["meta"]) == ("It <i>begins</i>.", "50 min · 4 May 2019")
    assert (watched["tick"], watched["state"]) == (True, None)
    assert (begun["state"], begun["meta"]) == ("Continue", "50 min · 30 min left")
    # Only the episode Play plays is the one to continue.
    assert (other["state"], other["about"], other["more"]) == ("In progress", None, 0)
    assert (unaired["state"], unaired["meta"]) == (None, "50 min · Airs 2 Jan 2999")
    # Each fact is held whole, so a narrow row breaks only between them.
    assert watched["facts"] == ["50 min", "4 May 2019"]
    assert begun["facts"] == ["50 min", "30 min left"]


def test_a_row_is_one_link_whose_description_is_not_in_its_name() -> None:
    for row in _run("seasonTexts")["rows"][:2]:
        assert (row["describes"], row["inName"], row["more"]) == (True, False, 1)
        assert row["controlsInLink"] == 0


def test_a_season_says_its_year_its_episodes_and_its_own_words() -> None:
    out = _run("seasonTexts")
    assert out["opened"] == {"facts": "2019 · 4 episodes", "text": "Its <b>second</b> year."}
    told = {"facts": "2017 · 1 episode", "text": "Told by another."}
    assert out["picked"] == told
    # Told by the season's own answer alone, not by the title loaded again.
    assert out["marked"] == {"season": told, "about": "Nine."}
    assert (out["older"], out["bare"]) == ({"facts": "1 episode", "text": None}, 0)


def test_the_rest_of_a_seasons_words_opens_and_closes_by_a_press() -> None:
    closed = [False, "More", "false", "More about Season 2"]
    opened = [True, "Less", "true", "Less about Season 2"]
    assert _run("seasonTexts")["press"] == [closed, opened, closed]


def test_a_text_opened_stays_open_as_its_season_is_drawn_again_and_no_longer() -> None:
    out = _run("openedTexts")
    assert out["pressed"] == [True, False, True]
    assert out["marked"] == [[True, False, True], True, True, "Less", "true"]
    assert out["closed"] == [True, False, False]
    assert out["other"] == [False, False, False]


def test_a_seasons_words_come_into_the_rows_on_show_and_leave_the_focus() -> None:
    out = _run("lateWords")
    said = [["50 min · 4 May 2019", "About one.", 1], ["50 min", None, 0]]
    assert (out["before"]["text"], out["before"]["rows"][0]) == (None, ["50 min", None, 0])
    assert out["came"] == {"facts": "2017 · 2 episodes", "text": "Words on 2", "rows": said}
    assert out["same"] == [True, True, 1]
    assert out["picked"] == [None, [2, 1]]
    assert out["pickedCame"] == {"facts": "2016 · 2 episodes", "text": "Words on 1", "rows": said}


def test_a_seasons_words_wait_for_a_press_to_end() -> None:
    out = _run("lateWords")
    assert (out["held"], out["letGo"]) == (None, "Words on 2")


def test_a_press_whose_end_never_comes_holds_a_seasons_words_back_only_so_long() -> None:
    assert _run("lateWords")["unended"] == [None, "Words on 2", 0]


def test_a_mark_made_while_a_seasons_words_come_outlives_them() -> None:
    held = {
        "eye": "true",
        "about": "About it.",
        "season": "Told.",
        "facts": "2017 · 2 episodes",
        "left": "50 min · 10 min left",
    }
    assert _run("markBeforeWords") == {"own": [held, held], "picked": [held, held]}


def test_words_of_a_season_left_or_that_never_come_change_nothing() -> None:
    out = _run("lateWords")
    assert out["left"] is None
    assert out["failed"] == {
        "facts": "2 episodes",
        "text": None,
        "rows": [["50 min", None, 0], ["50 min", None, 0]],
    }


def test_the_places_of_an_admin_s_facts_and_of_the_progress_are_held_while_a_title_loads() -> None:
    out = _run("loadingPlaces")
    places = {"admin": [5, 1], "viewer": [0, 0], "elsewhere": [0, 0]}
    assert {who: out[who] for who in places} == places


def test_a_leaving_show_holds_the_places_of_its_own_wider_words() -> None:
    leaving = _run("loadingPlaces")["leaving"]
    assert leaving["film"] == ["Leaving", 0, "200px"]
    assert leaving["show"] == ["Season 1 leaves", 1, "234px"]


def test_a_title_its_card_says_is_in_the_library_loads_as_one() -> None:
    held, not_held, leaving, admins = _run("loadingPlaces")["trending"]
    # The admin's facts, the buttons, a Keep of the viewer's own, the line that says it leaves.
    assert (held, not_held) == ([5, 3, 0, 0], [0, 2, 0, 0])
    assert (leaving, admins) == ([0, 4, 1, 1], [5, 3, 0, 1])


def test_rows_to_come_stand_in_until_a_season_loads_or_fails() -> None:
    out = _run("seasonTexts")
    # Nor is what they stand over pressed or reached by Tab.
    assert out["wait"] == [True, True, "true", True, True]
    assert out["failed"] == {
        "waits": [False, False, "false", False, False],
        "season": {"facts": "2019 · 4 episodes", "text": "Its <b>second</b> year."},
    }


def test_loading_a_title_again_keeps_the_season_the_person_picked_until_it_is_gone() -> None:
    out = _run("seasonPickedThenRefreshed")
    first = {"on": ["Season 1"], "eps": ["One One"], "label": "Season 1 episodes"}
    assert out["loaded"] == {"during": first, "kept": first, "deleted": ["Two One"]}
    # Still loading: the pill says what is coming, over the season that was on show.
    assert out["loading"]["during"] == {
        "on": ["Season 1"],
        "eps": ["Two One"],
        "label": "Season 2 episodes",
    }
    assert (out["loading"]["kept"], out["loading"]["deleted"]) == (first, ["Two One"])


def test_undo_in_a_toast_puts_focus_on_keep() -> None:
    assert _run("undoInToast") == {"pressed": "admin-keep", "reloaded": "admin-keep"}


def test_undo_of_a_keep_puts_focus_on_the_keep_button_that_comes_back() -> None:
    out = _run("undoKeepFocus")
    assert out["waits"] == ["primary", "keep"]
    assert out["movedOn"] == ["primary", "trailer"]


def test_a_shorter_season_leaves_its_pill_where_it_was_pressed() -> None:
    out = _run("shorterSeason")
    assert out["1"] == {"scroll": 530, "room": "30px"}
    assert out["2"] == {"scroll": 530, "room": ""}


def test_a_season_whose_leaving_line_goes_or_comes_leaves_its_pill_where_it_was_pressed() -> None:
    out = _run("leavingLineGoes")
    for at, scrolled in (("0", 0), ("25", 0), ("300", 260)):
        assert out[at]["gone"] == {"moved": 0, "line": False, "scroll": scrolled}
        assert out[at]["loaded"] == 0
        assert (out[at]["back"]["moved"], out[at]["back"]["line"]) == (0, True)
        assert out[at]["room"] == 0


def test_the_seasons_are_one_line_that_shows_the_picked_one() -> None:
    out = _run("seasonLine")
    assert (out["started"], out["scroller"]) == ([True, True], True)
    assert out["opened"] == [{"left": 300, "behavior": "auto"}]
    assert out["inView"] == []
    assert out["cutOff"] == [{"left": -68, "behavior": "smooth"}]
    assert out["redrawn"] == {"scroll": 77, "moves": [], "started": 2}


def test_a_viewer_never_asks_for_admin_facts() -> None:
    out = _run("viewerNoAdmin")
    assert not any("/admin/" in g for g in out["gets"])
    assert out["facts"] is False
    assert out["keep"] is True


def test_the_season_delete_of_a_title_page_is_given_how_many_seasons_there_are() -> None:
    out = _run("adminSeasonDelete")
    assert out["calls"] == [["300", "Brightwater", 5]]
    # Focused first, for the dialog to give it back: a click does not focus it in every browser.
    assert out["focused"] == ["admin-delete"]


def test_an_admin_sees_the_facts_and_keeps_or_deletes_through_the_admin_actions() -> None:
    out = _run("adminFacts")
    assert "/api/watch/admin/title/movie-120" in out["gets"]
    facts = out["facts"]
    assert facts[facts.index("Size on disk") + 1] == "16.2 GB"
    assert facts[facts.index("Last watched by anyone") + 1] == "Never"
    assert facts[facts.index("Deletion") + 1].startswith("27 Nov 2026")
    assert facts[facts.index("Keep") + 1] == "Not kept"
    assert out["rowKeep"] is False
    assert out["menuOpen"] is True
    assert out["calls"] == [["keep", "120", "30 days", True], ["delete", "120", "Glass Harbour"]]
    assert out["refetched"] == 2
    assert out["openAfterKeep"] is True
    assert out["closedAfterDelete"] is True
    assert out["admin"] is True
    assert out["director"] == "DirectorAda Rimmington"
    assert out["tagline"] == "Some secrets are worth keeping."
    assert "Metascore 74" in out["ratings"]


def test_the_admin_s_facts_have_their_place_before_they_come_and_say_so_when_they_fail() -> None:
    out = _run("factsLate")
    labels = ["Size on disk", "Added", "Last watched by anyone", "Deletion", "Keep"]
    known = {
        "columns": True,
        "labels": labels,
        "busy": False,
        "buttons": ["admin-keep", "admin-delete"],
        "error": None,
    }
    assert out["pending"] == {**known, "busy": True, "buttons": []}
    assert out["known"] == known
    failed = {**known, "labels": [], "buttons": ["facts-retry"], "error": "alert"}
    assert out["failed"] == failed
    assert (out["retried"], out["focus"]) == (known, "admin-keep")
    # What was known stays, said to be old.
    assert (out["stale"]["labels"], out["stale"]["error"]) == (labels, "alert")
    assert "facts-retry" in out["stale"]["buttons"]
    # A title no scan has seen yet: trying again cannot help.
    assert out["unknown"] == {**failed, "buttons": []}


def test_a_title_that_arrives_moves_neither_its_name_nor_its_buttons() -> None:
    out = _run("arrival")
    held = {"tagline": "held", "first": "ttl__actions"}
    for run in (out["tagline"], out["without"]):
        assert (run["loading"], run["loaded"]) == (held, held)
        assert "Leaving in 3 days" in run["leaving"]
        # An empty tagline gives its place to the meta line, so one that is to come is drawn.
        assert run["bars"] == [1, 0]


def test_deleting_a_title_opened_from_its_card_moves_focus_to_a_neighbour_without_scrolling() -> (
    None
):
    out = _run("deleteFromCard")
    assert out["next"] == {"closed": True, "focus": "movie-121", "preventScroll": True}
    assert out["previous"]["focus"] == "movie-119"
    assert (out["only"]["focus"], out["only"]["preventScroll"]) == ("watch-search", True)


def test_without_the_admin_actions_an_admin_keeps_through_the_shared_keep() -> None:
    assert _run("adminWithoutApi") == {"facts": True, "foot": False, "keep": True}


def test_share_says_copied_with_one_timer_then_goes_back() -> None:
    out = _run("share")
    assert out["copied"] == "Copied"
    assert (out["timersSet"], out["timersAfterSecond"]) == (1, 1)
    assert out["after"] == "Share"


def test_what_a_share_came_to_is_announced() -> None:
    out = _run("shareSaid")
    assert out["copied"] == {"before": "", "said": "Link copied.", "again": ""}
    assert "Settings" in out["refused"]["said"]


def test_share_holds_one_width_through_sharing_and_copied() -> None:
    states = _run("shareWidth")
    assert [s["text"] for s in states] == ["Share", "Sharing…", "Copied"]
    # Every state lays out the longest label unseen, so the button never resizes.
    longest = max((s["text"] for s in states), key=len)
    assert {s["widest"] for s in states} == {longest}
    # The icon and the label sit in one group, which is all the button holds: the group
    # centres inside the held width, so the label is never spaced away from its icon.
    assert all(
        s["group"] == "ttl__share-group" and s["inButton"] and s["groups"] == 1 for s in states
    )
    assert [s["icon"] for s in states] == [
        "#i-share-nodes",
        "#i-share-nodes",
        "#i-check",
    ]


def test_share_without_the_clipboard_shows_the_link_to_copy() -> None:
    out = _run("shareClipboardFails")
    assert out == {
        "value": "https://example.test/download/tok",
        "readOnly": True,
        "focused": True,
        "label": "Share",
    }


def test_share_refused_by_the_server_says_why_in_plain_words() -> None:
    out = _run("shareRefused")
    assert any("Settings" in n for n in out["note"])
    assert out["label"] == "Share"


def test_sharing_again_takes_away_what_the_last_share_said() -> None:
    out = _run("shareAgain")
    for case in ("refused", "shown"):
        assert out[case] == {"stale": 1, "label": "Copied", "left": 0, "focus": True}


def test_the_bar_of_close_is_marked_once_the_titles_text_reaches_it() -> None:
    assert _run("scrolledPastHead") == [False, False, True, True, False]


def test_the_bar_of_close_is_marked_anew_when_the_window_turns() -> None:
    assert _run("turnedPastHead") == [True, False, True]


def test_a_link_that_names_a_season_opens_the_show_on_it() -> None:
    out = _run("openOnSeason")
    assert out["named"] == {"on": ["Season 1"], "first": True, "asked": 1}
    own = {"on": ["Season 2"], "first": False, "asked": 0}
    assert (out["own"], out["none"], out["gone"]) == (own, own, own)
    assert out["failed"] == {**own, "asked": 1}


def test_a_library_title_says_what_its_own_page_said_of_it() -> None:
    out = _run("ownPageFacts")
    show = out["show"]
    # An episode seen before and begun again says both, and plays on from where it is.
    assert (show["href"], show["bar"]) == ("/watch/play/21?resume=1", True)
    assert (show["state"], show["name"]) == ("In progress", "Spring Tide, watched")
    # A watched season's tab keeps its words and adds an eye, and says so to a screen reader.
    assert show["tab"] == ["Season 1, watched", "Season 1", 1]
    # The season tabs stand with the episodes, not on the picture.
    assert show["tabsInHero"] == 0
    assert show["toggles"] == [
        "Mark show as watched",
        "Mark season as watched",
        "Mark Spring Tide as watched",
    ]
    assert out["left"] == "31 min left"
    assert out["gone"] == "This title is not available any more."


def test_an_address_that_names_a_season_opens_the_show_on_it() -> None:
    assert _run("openOnSeasonByAddress") == {
        "open": True,
        "title": "/api/watch/title/show-300",
        "on": ["Season 1"],
        "first": True,
    }


def test_the_bar_of_close_is_left_alone_while_a_title_goes() -> None:
    assert _run("turnedWhileLeaving") is False


def test_each_face_of_the_watched_button_holds_its_own_icon() -> None:
    assert _run("markedSeasonStays")["faces"] == [
        [1, "Mark season as watched"],
        [1, "Watched"],
    ]


def test_a_show_waits_only_so_long_for_the_season_its_link_names() -> None:
    quiet = _run("openOnSlowSeason")["quiet"]
    assert (quiet["held"]["busy"], quiet["bound"]) == (True, 1500)
    assert quiet["arrived"] == {
        "busy": False,
        "on": ["Season 2"],
        "episode": "Two One",
        "keep": None,
    }


def test_the_named_season_takes_over_when_it_comes_late_and_focus_stays() -> None:
    quiet = _run("openOnSlowSeason")["quiet"]
    assert quiet["came"] == {
        "busy": False,
        "on": ["Season 1"],
        "episode": "One One",
        "keep": None,
    }
    assert quiet["focusKept"] is True


def test_a_late_season_does_not_take_over_from_what_the_person_began() -> None:
    out = _run("openOnSlowSeason")
    for case in ("pressed", "reading", "facts"):
        assert out[case]["came"] == out[case]["arrived"]
        assert out[case]["focusKept"] is True


def test_keep_is_of_the_season_on_show_and_a_late_one_never_changes_it() -> None:
    out = _run("openOnSlowSeason")
    assert out["appears"]["arrived"]["keep"] is None
    assert out["appears"]["came"]["keep"] == "Keep Brightwater, Season 1"
    for case in ("otherKeep", "failed"):
        assert out[case]["came"]["on"] == ["Season 2"]
        assert out[case]["came"]["keep"] == "Keep Brightwater, Season 2"


def test_an_admin_s_late_season_that_leaves_takes_over_and_keep_is_of_it() -> None:
    admin = _run("openOnSlowSeason")["admin"]
    assert (admin["arrived"]["on"], admin["came"]["on"]) == (["Season 2"], ["Season 1"])
    assert admin["kept"] == "3010"


def test_a_late_season_does_not_take_over_under_an_open_keep_menu() -> None:
    out = _run("openOnSlowSeason")
    assert out["adminMenu"]["came"] == out["adminMenu"]["arrived"]
    assert (out["adminMenu"]["menuOpen"], out["adminMenu"]["kept"]) == (True, "")
    assert out["keepMenu"]["came"] == out["keepMenu"]["arrived"]


def test_a_link_to_a_shows_episodes_opens_it_at_them_unless_the_person_has_moved_on() -> None:
    out = _run("openAtEpisodes")
    assert out["show"] == {"to": 876, "focus": "Episodes", "hash": "#title=show-300"}
    assert (out["scrolled"]["to"], out["scrolled"]["focus"]) == (None, "Brightwater")
    assert (out["film"]["to"], out["film"]["focus"]) == (None, "Glass Harbour")


def test_tab_stays_inside_the_overlay() -> None:
    out = _run("focusTrap")
    assert (out["wrappedToFirst"], out["wrappedToLast"]) == (True, True)
    assert out["prevented"] == [True, True]
    assert out["pulledBack"] is True
    assert (out["held"], out["freed"]) == (True, True)


def test_safe_path_keeps_only_same_site_paths_and_refuses_ones_with_tabs_or_newlines() -> None:
    out = dict(_run("safePath"))
    assert out.pop("/watch/movie/1") == "/watch/movie/1"
    for refused in ("/\t/evil.example", "/\n/evil.example", "/\r/evil.example", "/watch\t/x"):
        assert out.pop(refused) == "", refused
    assert set(out.values()) == {""}


PLEX_ART = "/api/poster/120?kind=art"
BACKDROP = "/api/watch/img/tmdb/w1280/a.jpg"
POSTER = "/api/poster/120"


def test_a_library_title_shows_the_banners_picture_then_the_backdrop_then_the_poster() -> None:
    out = _run("pictures")

    assert {name: run["first"]["art"] for name, run in out.items()} == dict.fromkeys(out, PLEX_ART)
    assert out["plex"]["end"] == {"art": PLEX_ART, "fill": None, "poster": None, "loaded": True}
    assert out["tmdb"]["end"] == {"art": BACKDROP, "fill": None, "poster": None, "loaded": True}
    stand_in = {"art": None, "fill": POSTER, "poster": POSTER, "loaded": False}
    assert out["poster"]["end"] == stand_in


def test_art_that_is_no_wide_picture_gives_way_to_the_backdrop_before_the_poster() -> None:
    out = _run("pictures")

    assert out["unfit"]["end"] == {"art": BACKDROP, "fill": None, "poster": None, "loaded": True}
    assert out["unfitBoth"]["end"] == {
        "art": None,
        "fill": POSTER,
        "poster": POSTER,
        "loaded": False,
    }


def test_the_heads_height_is_settled_before_the_buttons_come_and_stays() -> None:
    out = _run("headHeight")
    waiting = {"buttons": False, "short": False, "late": False}
    tall = {"buttons": True, "short": False, "late": False}
    late = {"buttons": True, "short": False, "late": True}

    assert out["none"] == [waiting, waiting, {"buttons": True, "short": True, "late": False}]
    assert out["slow"] == [waiting, late, late, late]
    assert out["wide"] == [waiting, tall]
    assert out["failed"] is True


def test_a_title_outside_the_library_never_stretches_its_poster() -> None:
    out = _run("picturesOutsideTheLibrary")

    assert out["backdrop"]["art"] == BACKDROP
    poster = "/api/watch/img/tmdb/w500/p.jpg"
    assert (out["posterOnly"]["art"], out["posterOnly"]["poster"]) == (None, poster)
    assert (out["none"]["art"], out["none"]["poster"]) == (None, None)


def test_a_title_scanned_while_its_trailer_plays_is_not_opened_again_by_back() -> None:
    out = _run("scannedDuringTrailer")
    assert out["loaded"] == "#title=movie-120"
    assert out["afterBack"] == {
        "hash": "#title=movie-120",
        "trailer": False,
        "busy": None,
        "again": 0,
    }
    assert (out["closed"], out["end"]) == (True, "")


def test_a_library_show_has_a_toggle_for_itself_its_season_and_each_episode() -> None:
    out = _run("marks")
    assert out["opened"] == [
        ["show/300", "false", None],
        ["season/302", "false", "show-300"],
        ["episode/21", "false", "show-300"],
    ]
    assert out["picked"][1:] == [
        ["season/301", "false", "show-300"],
        ["episode/11", "false", "show-300"],
    ]
    # A button inside a link is neither.
    assert out["inLink"] == 0


def test_a_title_outside_the_library_has_no_toggle() -> None:
    assert _run("marks")["outside"] == 0


def test_a_mark_loads_the_title_again_with_the_season_on_show_and_keeps_the_focus() -> None:
    out = _run("marks")
    assert out["otherTitle"] == 0
    after = out["after"]
    assert [t[:2] for t in after["toggles"]] == [
        ["show/300", "false"],
        ["season/301", "true"],
        ["episode/11", "true"],
    ]
    assert (after["play"], after["episode"]) == ("Play S1 · E2", "Ep 11, watched")
    assert after["tab"] == "Season 1, watched"
    assert after["focus"] == "episode/11"
    assert after["painted"] == [["show/300", False]]


def test_a_season_marked_watched_stays_on_show_under_the_pointer_and_only_play_moves_on() -> None:
    out = _run("markedSeasonStays")
    assert (out["before"]["on"], out["before"]["play"]) == (["1"], "Resume S1 · E1")
    assert out["after"] == {
        "on": ["1"],
        "eps": ["Ep 11, watched"],
        "play": "Play S2 · E1",
        "leaving": True,
        "pressed": "true",
    }
    assert (out["moved"], out["focus"]) == (0, True)


def test_a_card_that_is_gone_is_let_go_with_the_page_it_stood_on() -> None:
    assert _run("cardsThatGo") == {"held": 0}


def test_a_persons_card_goes_with_the_title_it_stands_on() -> None:
    out = _run("cardOfAPersonGoes")
    assert out["title"] == [True, False]
    assert out["closed"] == [True, False]


SECOND = ["3020", "Keep Brightwater, Season 2", "Season 2 leaves in 9 days."]
THIRD = ["3030", "Keep Brightwater, Season 3", "Season 3 leaves in 2 days."]


def _keeps(shown: list) -> list:
    return [*shown[:2], shown[2] and shown[2].split(" Keep")[0]]


def test_a_title_opened_from_a_leaving_season_keeps_that_season_and_no_other() -> None:
    out = _run("leavingSeasons")
    assert _keeps(out["second"]) == SECOND
    assert _keeps(out["third"]) == THIRD
    assert out["gone"] == [None, None, None]


def test_a_title_opened_otherwise_names_the_season_it_keeps() -> None:
    assert _keeps(_run("leavingSeasons")["plain"]) == THIRD


def test_the_season_picked_in_the_list_is_the_one_kept() -> None:
    out = _run("leavingSeasons")
    assert _keeps(out["picked"]) == SECOND
    assert out["focus"] == "pill-2"
    assert out["pickedOther"] == [None, None, None]


def test_a_films_keep_names_no_season() -> None:
    out = _run("leavingSeasons")
    assert _keeps(out["film"]) == ["120", "Keep Glass Harbour", "Leaving in 3 days."]


def test_a_title_past_its_date_leaves_any_day_in_its_line_and_in_the_admins_facts() -> None:
    assert _run("leavingSeasons")["past"].startswith("Leaving any day. Keep it")
    facts = _run("factsPastTheDate")
    assert facts["film"] == "20 Nov 2026Leaving any day"
    assert facts["season"] == "20 Nov 2026Season 1, leaving any day"


def test_an_admins_keep_and_deletion_date_are_of_the_season_the_title_names() -> None:
    out = _run("leavingSeasonsAdmin")
    third, second, none = out["rows"]
    assert third.startswith("9 Dec")
    assert third.endswith("Season 3, in 9 days")
    assert second.startswith("2 Dec")
    assert second.endswith("Season 2, in 2 days")
    assert none == second
    assert out["calls"] == [
        ["keep", "3030", "30 days"],
        ["keep", "3020", "30 days"],
        ["keep", "", "30 days"],
    ]


def test_an_admins_open_keep_menu_closes_when_another_season_comes_in() -> None:
    out = _run("leavingSeasonsAdmin")
    assert out["menuAfter"] == {"open": False, "focus": "admin-keep", "live": True}


def test_removing_the_keep_of_a_show_asks_which_and_takes_no_season_of_its_own() -> None:
    assert _run("leavingSeasonsAdmin")["removed"] == [["keep", "", None]]


def test_an_open_keep_menu_closes_when_the_actions_are_for_another_season() -> None:
    out = _run("keepMenuAcrossSeasons")
    assert out["2"] == {
        "before": True,
        "after": False,
        "focus": "keep",
        "live": True,
        "label": "Keep Brightwater, Season 2",
        "posts": [],
    }
    assert (out["1"]["after"], out["1"]["live"], out["1"]["posts"]) == (False, True, [])


def test_a_keep_undone_elsewhere_shows_on_the_open_title() -> None:
    out = _run("keptElsewhere")
    assert out["before"] == [None, False]
    assert out["undone"] == ["3010", True]
    assert out["other"] == ["3010", True]
    assert out["drawnAgain"] is False
    assert out["askedWhenClosed"] == 0


def test_the_director_leads_to_their_titles_and_back() -> None:
    assert _run("directorLink") == {
        "text": "DirectorAda Rimmington",
        "href": "/?person=9008",
        "modified": False,
        "explored": [{"person": 9008}],
        "focus": "director",
        "plain": ["DirectorAda Rimmington", False],
    }


def test_ratings_that_come_late_take_the_places_held_for_them_and_reach_no_other_title() -> None:
    out = _run("lateRatings")
    # Their line is held, with no stand-in for a rating that may never come.
    assert out["before"] == {"shown": ["★TMDB 7.4"], "line": True, "held": True, "waiting": []}
    assert out["asked"] == 1
    shown = ["★TMDB 7.4", "★IMDb 8.3", "🍅RT 91%"]
    assert out["after"] == {"shown": shown, "line": True, "held": False, "waiting": []}
    assert out["failed"] == {"shown": ["★TMDB 7.4"], "line": True, "held": False, "waiting": []}
    assert out["renewed"] == ["★IMDb 8.2", "★TMDB 7.4", "Metascore 70"]
    assert out["brief"] == ["Meta", "true"]
    assert out["moved"] == "★IMDb 5.0"
    assert out["others"] == ["/api/watch/title/movie-120/ratings"]


def test_no_place_is_left_for_ratings_when_none_came() -> None:
    out = _run("lateRatings")
    assert (out["held"]["line"], out["held"]["held"]) == (True, True)
    assert out["empty"] == {"shown": [], "line": False, "held": False, "waiting": []}


def test_late_ratings_are_no_longer_asked_for_once_the_title_is_left_and_again_when_it_is_back() -> (
    None
):
    out = _run("lateRatings")
    assert out["dropped"] == [True, True]
    assert out["again"] == [2, True]


def test_a_title_s_thumbs_stand_between_its_actions_and_its_overview_from_the_start() -> None:
    out = _run("thumbs")
    assert (out["skel"], out["waits"]) == (True, False)
    order = out["order"]
    assert order.index("ttl__actions") < order.index("rate") < order.index("ttl__overview")
    assert out["rated"] == ["false", "false", "true", "false"]


def test_a_thumb_pressed_stays_on_and_keeps_the_focus_when_the_title_is_drawn_again() -> None:
    out = _run("thumbs")
    loved = ["false", "false", "false", "true"]
    assert out["pressed"] == [
        loved,
        [{"url": "/api/watch/ratings/movie-120", "body": {"level": 2}}],
    ]
    assert out["redrawn"] == [True, True, loved]


def test_the_thumbs_place_is_held_where_they_will_be_while_a_title_loads() -> None:
    assert _run("thumbsWait") == {
        "admin": {"columns": True, "buttons": 3},
        "viewer": {"columns": False, "buttons": 3},
        "tmdb": {"columns": False, "buttons": 2},
    }


def test_thumbs_go_with_their_title() -> None:
    out = _run("thumbs")
    assert out["next"] == ["false"] * 4
    assert out["old"] == "false"
    assert out["back"] == ["true", "false", "false", "false"]
    assert out["closed"] == out["back"]


def test_a_title_that_cannot_be_rated_has_no_thumbs_and_waits_for_that_only_so_long() -> None:
    out = _run("thumbsNone")
    assert out["before"] is False
    assert out["late"] == [True, True]
    assert out["told"] is True
    assert out["again"] == [True, False]


def test_a_film_queued_elsewhere_on_the_page_is_queued_in_its_panel() -> None:
    assert _run("queuedElsewhere") == {
        "before": "Download",
        "after": "Queued",
        "polls": True,
        "events": 1,
    }


def test_a_title_still_looked_for_says_so_as_its_card_does() -> None:
    queued = ["Queued", "Waiting for a download slot. Follow it in Downloads."]
    assert _run("lookedFor") == {
        "searching": ["Looking for the best version", "Follow it in Downloads."],
        "redrawn": False,  # nothing new: an open menu stays
        "stuck": [
            "Looking for the best version",
            "Nothing found yet · next attempt in ~4h. Follow it in Downloads.",
        ],
        "queued": queued,
        "other": queued,
    }


def test_a_page_drawn_on_its_way_leaves_no_trace_until_it_opens_as_a_press_would_open_it() -> None:
    out = _run("drawnThenOpened")
    drawn = {
        "hidden": False,
        "locked": True,
        "hash": "",
        "pushes": 0,
        "focus": False,
        "inert": False,
        "title": "Glass Harbour",
        "words": "A survey crew maps a drifting station.",
    }
    assert out["drawn"] == drawn
    assert out["back"] == {**drawn, "hidden": True, "locked": False, "words": None, "empty": 0}
    assert out["untouched"] == 0
    assert out["open"] == {
        **drawn,
        "hash": "#title=movie-120",
        "pushes": 1,
        "focus": True,
        "inert": True,
        "busy": None,
    }
    assert out["gets"] == 1
    assert out["closed"] == [True, True]


def test_a_page_drawn_in_full_is_not_drawn_again_and_asks_for_the_rest_only_once_open() -> None:
    assert _run("drawnInFull") == {
        "drawn": {"director": "DirectorAda Rimmington", "cast": 1, "gets": 1},
        "kept": True,
        "gets": ["/api/watch/title/movie-120", "/api/watch/title/movie-120/ratings"],
    }


def test_the_words_a_preview_was_read_by_stay_as_its_page_fills_in() -> None:
    out = _run("wordsGoOn")
    assert out["before"] == {"words": "What was read.", "play": "Play", "skels": 1, "busy": "true"}
    assert out["after"] == {
        "words": "What was read.",
        "director": "DirectorAda Rimmington",
        "busy": None,
        "faded": ["ttl__credit"],
        "swapped": False,
    }


def test_a_drawn_page_that_cannot_be_had_keeps_what_its_preview_said_and_can_be_tried_again() -> (
    None
):
    out = _run("drawnThenFailed")
    assert out["failed"] == {
        "line": "Couldn't load details",
        "words": "A survey crew maps a drifting station.",
        "play": "Play",
        "skels": 0,
    }
    assert out["then"] == {"line": None, "director": "DirectorAda Rimmington"}


def test_a_page_drawn_from_an_early_reply_opens_up_to_date_after_a_change_made_while_it_was_drawn() -> (
    None
):
    assert _run("changedWhileDrawn") == {
        "mm:watch-marked": "true",
        "mm:watch-kept": "true",
        "mm:title-changed": "true",
        "own": 1,
    }


def test_a_drawn_shows_download_waits_for_its_seasons_through_a_failed_try_and_then_works() -> None:
    assert _run("drawnDownloadWaits") == {
        "drawn": ["true", "true"],
        "failed": ["true", None, 0],
        "again": ["true", "true"],
        "came": [None, None, 1],
    }


def test_a_title_is_asked_for_ahead_once_one_at_a_time_few_a_minute_and_again_when_it_changes() -> (
    None
):
    assert _run("askedAhead") == {
        "oneAtATime": ["900"],
        "once": ["1"],
        "most": 10,
        "changed": ["2", "2"],
    }


def test_a_title_opened_while_it_downloads_shows_its_sizes_and_time_left_at_the_first_poll() -> (
    None
):
    before, after, events = _run("downloadingSizes")
    assert before == ""
    assert "3.1 GB of 7.4 GB" in after and "about 6 min left" in after
    assert events == 1  # the share did not move, so nothing else is told


def test_only_a_title_asked_for_on_the_chance_it_opens_says_so_to_the_server() -> None:
    assert _run("aheadMarked") == {"hovered": ["/api/watch/title/movie-120?ahead=1"], "drawn": 1}


def test_a_title_opened_on_what_was_asked_ahead_and_then_refused_is_asked_again_and_opens() -> None:
    assert _run("aheadRefusedOnOpen") == {
        "meanwhile": {"asked": 2, "failed": False},
        "director": "DirectorAda Rimmington",
        "failed": False,
    }


def test_a_refused_early_ask_stops_asking_ahead_for_a_minute_but_never_holds_back_an_opening() -> (
    None
):
    assert _run("aheadBacksOff") == {
        "meanwhile": 1,
        "drawn": 1,
        "after": ["/api/watch/title/movie-2?ahead=1"],
    }


def test_a_title_asked_ahead_and_not_found_is_not_asked_again_when_opened() -> None:
    assert _run("aheadNotFoundOnOpen") == {"asked": 1, "failed": True}


def test_a_trailer_whose_player_never_answers_or_cannot_play_goes_back_to_the_artwork() -> None:
    out = _run("trailerStuck")
    gone = {"playing": False, "told": ["Trailer unavailable."], "focus": "trailer"}
    # Asked again to report while it has not answered; not once it has.
    assert out["silent"] == {**gone, "asked": 2}
    assert out["refused"] == {**gone, "asked": 1}
    assert out["heard"] == {"playing": True, "told": [], "focus": None, "asked": 1}
    assert out["stopped"]["told"] == []
    # A frame slow to load is waited for: the player's wait starts once it has loaded.
    assert out["slowThenHeard"] == {
        "loaded": True,
        "playing": True,
        "told": [],
        "focus": None,
        "asked": 1,
    }
    assert out["slowThenSilent"] == {"loaded": True, **gone, "asked": 2}
    # One that never loads gives way in the end.
    assert out["neverLoads"] == {**gone, "asked": 0}


def test_a_trailer_shows_a_named_loading_mark_until_its_player_speaks_or_it_gives_way() -> None:
    out = _run("trailerLoadingMark")
    mark = {"text": "Loading trailer", "role": "status", "spinner": True}
    # From the moment the frame is added, through its load, until the player's first word.
    assert out["waiting"] == {"pressed": mark, "loaded": mark, "after": mark, "playing": True}
    for answer, playing in (("heard", True), ("refused", False)):
        assert out[answer] == {"pressed": mark, "loaded": mark, "after": None, "playing": playing}
    # Gone when the wait ends, whether the player stayed silent or the frame never loaded.
    for ended in ("silent", "never", "closed"):
        assert out[ended]["pressed"] == mark
        assert out[ended]["after"] is None
        assert out[ended]["playing"] is False


def test_a_title_s_page_brings_its_cards_up_to_what_it_learns_of_it_not_to_its_preview() -> None:
    assert _run("arrivalPaints") == {"drawn": [], "opened": [["movie/120", True]]}


def test_on_a_phone_an_admin_s_leaving_line_takes_them_to_the_keep_below() -> None:
    out = _run("leavingToKeep")
    words = "Leaving in 3 days. Keep it to stop the deletion."
    assert out["phone"] == {
        "words": words,
        "link": "Keep it to stop the deletion",
        "focus": "admin-keep",
    }
    # A phone on its side stacks the page too, so it is a link there as well.
    assert out["sideways"] == out["phone"]
    assert out["wide"]["link"] is None and out["viewer"]["link"] is None
    assert out["wide"]["words"] == words
    # Before the Keep has come there is nowhere to take the focus.
    assert out["waiting"]["link"] == "Keep it to stop the deletion"
    assert out["waiting"]["focus"] != "admin-keep"


def test_a_title_s_page_takes_its_rating_from_the_server_each_time_it_loads() -> None:
    assert _run("ratedElsewhere") == {"first": [1, 1], "again": [2, -2]}


def test_a_film_opened_before_radarr_answered_is_asked_once_more_and_drawn_in_place() -> None:
    out = _run("radarrLate")
    assert out["late"] == {
        "waits": 1,
        "asked": 2,
        "primary": "Queued",
        "focus": "primary",
        "told": 1,
    }
    # Radarr had answered: nothing more is asked.
    assert (out["answered"]["waits"], out["answered"]["asked"]) == (0, 1)
    # Another title opened meanwhile: the late answer is dropped.
    assert (out["moved"]["asked"], out["moved"]["told"]) == (2, 0)
    assert out["moved"]["primary"] == "Resume S2 · E4"
    # Download pressed meanwhile: what it started stands.
    assert (out["pressed"]["asked"], out["pressed"]["primary"]) == (1, "Queued")
