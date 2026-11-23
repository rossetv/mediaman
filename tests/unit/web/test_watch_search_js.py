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
    assert out["viewer"]["keep"] == 0
    assert out["admin"]["calls"] == 1
    assert out["admin"]["href"] == "/settings#setg-integrations"
    assert "Connect TMDB" in out["admin"]["text"]
    assert out["viewer"]["calls"] == 1
    assert out["viewer"]["href"] is None
    assert "tmdb" not in out["viewer"]["text"].lower()
    assert out["empty"] is True
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
