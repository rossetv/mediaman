"""What becomes of a dialog opened from a title as the title closes, changes or is drawn anew."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "title_dialogs_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_back_closes_a_dialog_with_the_title_it_was_opened_from() -> None:
    assert _run("back") == {
        "closed": 1,
        "inert": False,
        "at": "show-300",
        "forward": "admin-delete",
    }


def test_back_to_the_title_before_closes_a_dialog_of_the_one_after() -> None:
    assert _run("backToTitle") == {"closed": 1, "title": "Brightwater", "in": True}


def test_a_dialog_gives_the_focus_back_to_its_button_as_drawn_since_or_to_the_title() -> None:
    assert _run("redrawn") == [["admin-delete", True], ["ttl-title", True]]


def test_a_title_deleted_from_its_page_leaves_the_rows_and_its_other_cards_say_not_in_library() -> (
    None
):
    def want(tmdb: str) -> dict:
        return {
            "taken": ["own", "continue"],
            "at": "other",
            "badges": 0,
            "links": [[tmdb, f"/#title={tmdb}"]] * 2,
        }

    assert _run("deleted") == {"movie-120": want("tmdb-movie-77"), "show-300": want("tmdb-tv-77")}


def test_a_title_deleted_after_it_was_closed_leaves_the_page_and_forward_says_it_is_gone() -> None:
    for ref, out in _run("deletedAfterClosing").items():
        assert out["taken"] == ["banner", "own"], ref
        assert out["at"] == "ttl-title", ref
        assert out["forward"] == "This title is not available any more.", ref


def test_a_search_counts_what_is_left_of_it_and_the_last_one_leaves_the_focus_to_its_field() -> (
    None
):
    assert _run("deletedResult") == [["1 title", "movie-9"], ["No title matches", "watch-search"]]


def test_a_deleted_title_s_banner_above_the_screen_goes_without_moving_what_is_on_it() -> None:
    assert _run("bannerAbove") == {
        "folded": 0,
        "by": [-436],
        "at": 541,
        "closed": True,
        "there": False,
    }


def test_add_a_language_speaks_of_a_film_as_a_film_and_looks_for_a_show_s_whole_season() -> None:
    out = _run("languages")
    film, show = out["film"], out["show"]
    assert "episode" not in film["foot"] + film["lede"]
    assert film["search"] == ["/api/watch/languages/120/audio?language=pt&scope=movie"]
    assert show["foot"].startswith("Its next episode has")
    assert show["search"] == ["/api/watch/languages/41/audio?language=pt&scope=season"]


def test_a_copy_asked_for_from_a_title_page_speaks_of_nothing_playing_and_goes_back_there() -> None:
    out = _run("languagesAsked")
    onway = out["back"]["onway"]
    assert onway["back"] == "Back to Add a language"
    assert "Keep watching" not in " ".join(onway["facts"])
    assert onway["facts"][0] == "What you have plays until the new copy is in."
    # Back, and Done, go to the page's first view, never to the player's menu.
    assert out["back"]["after"] == out["done"]["after"] == "Add a language"
