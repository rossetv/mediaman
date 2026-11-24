"""The Watch page's two-group search, run under node against a fake DOM."""

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
    assert out["debounce"] == 350
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
    assert out["whileWaiting"] == ""
    assert out["line"] == "No matches for “zzqx”"
    assert out["status"] == "No matches for “zzqx”"
    assert out["tmdbOnly"] == {"msg": "", "heads": ["Available to download"]}


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
    assert out["whileLoading"] is True  # nothing to focus yet: the skeleton has no heading
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


def test_the_rows_and_results_fade_out_then_in_and_can_be_interrupted() -> None:
    out = _run("viewFade")
    # The rows fade out over --dur-fast; nothing is hidden until that ends.
    assert out["during"] == {"browse": False, "results": True, "anims": [["browse", 150]]}
    assert out["after"] == {"browse": True, "results": False, "fadeIn": True, "cancelled": True}
    assert out["clearing"] == {"anims": 2, "node": "results", "duration": 150}
    # Typing again before the fade ends puts the results back with no flash of the rows.
    assert out["reversed"] == {"results": False, "browse": True, "cancelled": True, "anims": 2}
    assert out["afterStale"] == {"results": False, "browse": True}
    assert out["quick"] == {"browse": False, "results": True, "cancelled": True}
    assert out["reduced"] == {"ran": 0, "browse": True, "results": False}


def test_a_new_search_while_the_last_results_fade_out_ends_in_one_clean_view() -> None:
    out = _run("settledAfterFade")
    # No match: the line, and no groups or placeholders left behind from the fade.
    assert out["nothing"]["heads"] == []
    assert out["nothing"]["skeleton"] == 0
    assert out["nothing"]["stacks"] == 0
    assert out["nothing"]["msg"].startswith("No matches for “zzqx”")
    # Two searches in a row: only the last one's results stand.
    assert out["chained"]["titles"] == ["Harrow"]
    assert out["chained"]["heads"] == ["In your library"]
    assert (out["chained"]["skeleton"], out["chained"]["stacks"]) == (0, 0)


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
    assert out["clear"] is False
    assert out["after"] == {"results": False, "browse": True, "titles": ["Orchard Keepers"]}
    assert out["cleared"] == {"results": True, "browse": False}
    assert out["none"] == {"skeleton": 0, "line": "No matches for “zzqx”"}


def test_a_download_badge_names_its_percentage_and_a_part_owned_title_is_in_the_library() -> None:
    out = _run("stateBadges")
    assert out["texts"] == ["Downloading 95%", "Downloading", "In library"]
    assert out["said"] == [["Downloading "], [], []]
    assert out["classes"][2].endswith("watch-badge--in")


def test_a_later_group_crosses_over_its_skeleton_and_old_results_do_not_fade_in_again() -> None:
    out = _run("swaps")
    # Moving a node restarts its CSS animation, so the old view must be rid of .fade-in first.
    assert out["easing"] > 0
    assert out["oldStill"] == 0
    assert out["slotBefore"] is True
    assert out["crossing"] == {"grid": "grid", "parts": 2, "skeletonInert": True, "cards": 1}
    assert out["settled"] == {"children": 1, "skeleton": 0, "cards": 1, "fadeIn": 0}
