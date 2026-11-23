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
    assert out["history"][-1] == ["back"]
    assert (out["leaving"], out["focusOnCard"]) == (True, True)
    assert (out["hidden"], out["unlocked"]) == (True, True)


def test_a_direct_link_opens_the_title_and_closing_drops_the_hash_without_history() -> None:
    out = _run("directLink")
    assert out["opened"]["hidden"] is False
    assert out["opened"]["gets"][0] == "/api/watch/title/movie-120"
    assert out["history"] == [["replace", "/watch"]]
    assert out["hash"] == ""


def test_an_old_recommendation_link_opens_the_pick() -> None:
    out = _run("oldRecommendationLink")
    assert out["history"][0] == ["replace", "/watch#title=rec-5"]
    assert out["gets"][0] == "/api/watch/title/rec-5"
    assert "two slow mysteries" in out["reason"]


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
    assert playing["button"] == "Close trailer"
    assert playing["src"] == (
        "https://www.youtube-nocookie.com/embed/abcdefghijk?autoplay=1&rel=0&playsinline=1"
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
        "button": "Trailer",
    }
    assert out["closedSecond"] is True


def test_a_title_without_a_trailer_has_no_trailer_button() -> None:
    assert _run("noTrailerKey") == {"trailer": False, "primary": "Download"}


def test_escape_handled_by_an_open_keep_menu_leaves_the_overlay_open() -> None:
    assert _run("keepMenuEscapeFirst")["open"] is True


def test_a_failed_load_says_so_with_try_again_and_close() -> None:
    out = _run("loadFails")
    assert out["failed"] == {"line": "Couldn't load details", "buttons": ["Try again", "Close"]}
    assert out["retried"]["text"] == "Download"


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
    assert out["events"] == [
        {"ref": "tmdb-movie-77", "download_state": "queued", "download_pct": None},
        {"ref": "tmdb-movie-77", "download_state": "downloading", "download_pct": 42},
        {"ref": "tmdb-movie-77", "download_state": "in_library", "download_pct": None},
    ]
    assert any("queued" in t for t in out["toasts"])


def test_a_queued_download_the_arr_no_longer_holds_gives_the_download_button_back() -> None:
    out = _run("queuedThenGoneOnReopen")["answered"]
    assert (out["before"], out["after"]) == ("Queued", "Download")
    assert out["events"][-1] == {
        "ref": "tmdb-movie-77",
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


def test_a_download_the_arr_already_has_shows_the_title_in_the_library_without_try_again() -> None:
    out = _run("downloadAlreadyThere")
    assert out["at409"]["primary"]["text"] == "In library"
    assert (out["at409"]["error"], out["at409"]["retry"]) == (False, False)
    assert out["events"][0] == {
        "ref": "tmdb-movie-77",
        "download_state": "in_library",
        "download_pct": None,
    }
    assert any("already in your library" in t for t in out["toasts"])
    # The title is asked for again and opens as the library title it now is.
    assert out["gets"][-1] == "/api/watch/title/movie-120"
    assert out["refetched"]["primary"]["text"] == "Play"
    assert out["refetched"]["retry"] is False


def test_a_pick_downloads_through_its_own_route_and_promises_the_email() -> None:
    out = _run("recDownload")
    assert out["posts"] == [{"url": "/api/recommended/5/download"}]
    assert out["email"] is True
    assert out["share"] is False


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
    assert facts[facts.index("Last watched") + 1] == "Never"
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


def test_share_holds_one_width_through_sharing_and_copied() -> None:
    states = _run("shareWidth")
    assert [s["text"] for s in states] == ["Share", "Sharing…", "Copied"]
    # Every state lays out the longest label unseen, so the button never resizes.
    longest = max((s["text"] for s in states), key=len)
    assert {s["widest"] for s in states} == {longest}


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
