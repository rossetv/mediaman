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


def test_a_card_opens_at_once_with_its_title_and_its_own_address() -> None:
    out = _run("openFromCard")
    assert out["afterModifiers"] == {"hidden": True, "gets": 0}
    loading = out["loading"]
    assert (loading["hidden"], loading["title"], loading["busy"]) == (False, "Meridian", "true")
    assert loading["focus"] is True
    assert loading["locked"] is True
    assert loading["hash"] == "#title=tmdb-movie-77"
    assert loading["history"] == [["push", "/watch#title=tmdb-movie-77"]]
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
    assert out["hrefs"] == ["/watch?genre=drama", "/watch?genre=science-fiction"]
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
    assert out["history"] == [["replace", "/watch"]]
    assert out["hash"] == ""


def test_a_title_no_card_opened_gives_the_focus_to_one_of_its_cards_that_shows() -> None:
    assert _run("directLinkFocus") == {"library": "shown", "scanned": "tmdb-movie-77"}


def test_an_old_recommendation_link_opens_the_pick() -> None:
    out = _run("oldRecommendationLink")
    assert out["history"][0] == ["replace", "/watch#title=rec-5"]
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
        "button": ["Trailer", False],
        "focus": True,
        "tabbable": False,
    }
    assert out["closedSecond"] is True


def test_a_playing_trailer_is_left_from_its_bar_and_the_row_of_buttons_stays() -> None:
    playing = _run("escapeOrder")["playing"]
    assert playing["back"] == "Back to details"
    assert (playing["focus"], playing["tabbable"]) == (True, True)
    assert playing["row"] == ["Download", "Trailer"]
    assert playing["button"] == ["Trailer", True]


def test_back_closes_the_trailer_before_the_title_and_the_x_closes_both() -> None:
    out = _run("trailerHistory")
    title = "#title=tmdb-movie-77"
    details = {"open": True, "trailer": False, "hash": title}
    gone = {"open": False, "trailer": False, "hash": ""}
    assert out["back"] == details
    assert out["forward"] == {**details, "trailer": True}
    assert out["backTwice"] == gone
    assert out["button"] == {**details, "asked": 1}
    assert (out["close"], out["closeLink"]) == (gone, gone)
    # Loaded again mid-trailer: the title alone, and one Back still leaves it.
    assert (out["reload"], out["reloadBack"]) == (details, gone)


def test_a_trailer_that_ends_gives_way_to_the_artwork() -> None:
    out = _run("trailerEnds")
    to = "https://www.youtube-nocookie.com"
    assert out["ended"]["playing"] is False
    # Asked to report, then paused, so that it is silent while it fades.
    assert out["ended"]["sent"] == [["listening", None, to], ["command", "pauseVideo", to]]
    assert out["ended"]["focus"] == "trailer"
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
        assert run["toasts"] == ["'Meridian' is already in Radarr"], name
    event = {"ref": "tmdb-movie-77", "title": "Meridian", "kind": "movie", "year": 2026}
    queued = out["queued"]
    assert queued["refetched"]["primary"]["text"] == "Queued"
    assert queued["events"] == [{**event, "download_state": "queued", "download_pct": None}]
    assert queued["polls"] == 1
    downloading = out["downloading"]
    assert downloading["refetched"]["primary"]["text"] == "Downloading 40%"
    assert downloading["events"] == [{**event, "download_state": "downloading", "download_pct": 40}]
    # Radarr has it but lists nothing: Download would only 409 again.
    assert out["none"]["refetched"]["primary"]["text"] == "Already in Radarr"
    assert out["none"]["refetched"]["primary"]["disabled"] is False
    assert out["none"]["events"] == []
    assert out["failed"]["refetched"]["primary"]["text"] == "Already in Radarr"
    assert out["failed"]["refetched"]["primary"]["disabled"] is False
    assert out["failed"]["events"] == []
    # It is asked again, slowly, for as long as it is already there.
    for name in ("none", "noneYet", "failed"):
        assert (out[name]["polls"], out[name]["slowPolls"]) == (0, 1), name
    # Still none once Radarr answers later: it was removed, and can be downloaded.
    assert (out["none"]["later"], out["none"]["pollsLater"]) == ("Download", [0, 0])
    # No answer is no news.
    assert out["noneYet"]["later"] == "Already in Radarr"
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
    assert tracked["at409"] == {"primary": "Already in Sonarr", "retry": False}
    assert tracked["gets"].count("/api/watch/title/tmdb-tv-90") == 2
    assert (tracked["refetched"], tracked["retry"]) == ("Already in Sonarr", False)
    assert tracked["events"] == []
    assert (tracked["polls"], tracked["reopened"]) == (0, "Already in Sonarr")
    # Queued, coming in or all there: the panel and every card say it, once, before any reopening.
    event = {"ref": "tmdb-tv-90", "title": "Hollow Pines", "kind": "show", "year": 2026}
    for name, state, pct, text, polls in (
        ("queued", "queued", None, "Queued", 1),
        ("downloading", "downloading", 40, "Downloading 40%", 1),
        ("allThere", "in_library", None, "In library", 0),
    ):
        run = out[name]
        assert run["at409"]["primary"] == "Already in Sonarr", name
        assert (run["refetched"], run["reopened"]) == (text, text), name
        assert run["events"] == [{**event, "download_state": state, "download_pct": pct}], name
        assert run["polls"] == polls, name
        assert run["reopenedEvents"] == run["events"], name
    # The title asked for again is in the library: it opens as the library show.
    assert out["inLibrary"]["at409"]["primary"] == "Already in Sonarr"
    assert out["inLibrary"]["refetched"] == "Resume S2 · E4"
    assert out["inLibrary"]["events"] == [
        {**event, "download_state": "in_library", "download_pct": None}
    ]


def test_already_there_is_dropped_on_reopening_once_the_arr_answers() -> None:
    out = _run("trackedReopened")
    for kind, tracked, download in (
        ("movie", "Already in Radarr", "Download"),
        ("show", "Already in Sonarr", "Download 2 seasons"),
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


def test_a_show_sonarr_tracks_or_fully_held_offers_no_download() -> None:
    out = _run("tvBlocked")
    assert out["tracked"] == {
        "primary": {
            "text": "Already in Sonarr",
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


def test_deleting_from_a_list_row_moves_focus_to_a_neighbouring_row_without_scrolling() -> None:
    out = _run("deleteFromListRow")
    assert out["next"] == {
        "closed": True,
        "focus": "del-121",
        "tabindex": None,
        "preventScroll": True,
    }
    assert out["previous"]["focus"] == "del-119"
    assert (out["only"]["focus"], out["only"]["tabindex"]) == ("watch-list", "-1")
    assert out["only"]["preventScroll"] is True


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
        "href": "/watch?person=9008",
        "modified": False,
        "explored": [{"person": 9008}],
        "focus": "director",
        "plain": ["DirectorAda Rimmington", False],
    }
