"""Watch next, the endless grid under the last row of Watch, run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_next_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_first_page_is_asked_only_once_the_end_of_the_rows_is_near() -> None:
    out = _run("firstOnlyNear")
    assert (out["onLoad"], out["far"], out["near"]) == (0, 0, 1)
    assert (out["inReach"], out["short"]) == (0, 1)
    assert (out["type"], out["cursor"]) == ("all", None)
    # The rest of the row and two rows more, at three across.
    assert out["skels"] == 6


def test_one_page_at_a_time_each_from_the_cursor_the_last_gave() -> None:
    out = _run("oneAtATime")
    assert out["during"] == 1
    assert (out["after"], out["cursor"]) == (2, "c24")
    assert out["cards"] == 24


def test_a_title_the_grid_holds_is_not_added_again() -> None:
    assert _run("noTitleTwice") == [f"tmdb-movie-{n}" for n in range(10)]


def test_a_filter_starts_again_from_its_head_and_drops_the_late_answer() -> None:
    out = _run("filterDropsTheOldAnswer")
    assert (out["type"], out["cursor"]) == ("movie", None)
    assert out["dimmed"] is True
    assert out["kept"] == [f"tmdb-movie-{n}" for n in range(6)]
    assert out["refs"] == ["tmdb-movie-200", "tmdb-movie-201", "tmdb-movie-202"]
    assert out["pressed"] == ["false", "true", "false"]
    assert out["dim"] is False


def test_a_failure_holds_the_rest_of_the_row_and_tries_again_once_online() -> None:
    out = _run("failsThenOnline")
    # 24 cards in five columns: one to end the row, ten more.
    assert out["loading"] == 11
    assert "Couldn’t load more titles." in out["line"]
    assert "Try again" in out["line"]
    assert out["held"] == 1
    assert out["quiet"] == 2
    assert out["again"] == {"asked": 3, "cursor": "c24", "busy": "true"}
    assert (out["after"], out["cards"], out["static"]) == ("", 29, False)


def test_an_answer_that_failed_shows_the_line_with_what_it_brought_and_never_ends() -> None:
    out = _run("failedAnswer")
    assert out["cards"] == 27
    assert "Couldn’t load more titles." in out["line"] and "Try again" in out["line"]
    # 27 cards in five columns: the rest of the row is held under the line.
    assert out["held"] == 3
    assert out["quiet"] == 2
    assert out["asked"] == 6 and out["cursor"] == "c27"
    assert out["toasts"] == 4
    assert out["gone"] is False
    assert "Try again" in out["still"] and out["busy"] == "false"


def test_a_first_page_that_failed_says_no_titles_came_and_is_tried_again() -> None:
    out = _run("failedFirstPage")
    assert "Couldn’t load titles." in out["line"]
    assert out["gone"] is False
    assert out["asked"] == 2
    assert (out["after"], out["cards"]) == ("", 6)


def test_a_cursor_that_started_again_starts_the_grid_again_at_the_section() -> None:
    out = _run("restarted")
    assert out["deep"] > 5000
    assert (out["cards"], out["first"]) == (24, "tmdb-movie-0")
    assert out["top"]["top"] < out["deep"]
    assert out["cursor"] == "r24"


def test_too_many_requests_waits_and_asks_again_without_a_failure() -> None:
    out = _run("waitsAfterTooMany")
    assert out["waiting"] == {"line": "", "ms": [5000]}
    assert out["asked"] == 2


def test_it_stops_asking_once_the_sources_are_dry_and_goes_when_it_had_nothing() -> None:
    out = _run("stopsWhenDry")
    assert (out["asked"], out["gone"]) == (3, True)
    assert out["noTmdb"] == {"asked": 1, "gone": True}


def test_a_hidden_section_asks_for_nothing() -> None:
    assert _run("hiddenAsksNothing") == 0


def test_each_card_says_why_as_text_and_the_preview_has_it_in_full() -> None:
    out = _run("reasons")
    assert out["text"] == "Because you searched “<img src=x onerror=alert(1)>”"
    assert out["imgs"] == 1
    assert out["icon"] == "i-magnifying-glass"
    assert out["why"] == [
        "You searched <img src=x onerror=alert(1)>",
        "Your search",
        "i-magnifying-glass",
    ]
    # A pick's preview asks for the AI's words itself; its caption has no second sparkle.
    assert out["ai"] == {"line": True, "why": None, "sub": "Movie"}
    assert out["unknown"] is False
    assert out["note"] is True


def test_cards_arrive_a_step_later_per_column_and_row_but_not_on_a_flick() -> None:
    out = _run("arrival")
    assert out["pending"] is True
    assert out["delays"] == ["0ms", "32ms", "64ms", "70ms", "102ms", "134ms"]
    assert out["flicked"] == [[False, False]] * 3


def test_leaving_keeps_the_titles_for_this_entry_and_person() -> None:
    out = _run("keptOnLeaving")
    assert out == {
        "id": "e1",
        "who": "ann",
        "filter": "all",
        "cursor": "c6",
        "items": 6,
        "opened": "tmdb-movie-2",
    }


def test_only_the_latest_titles_are_kept_with_what_their_cards_need() -> None:
    out = _run("keptAreCapped")
    # 624 titles in rows of four: the first six rows go, and stand as their height.
    assert (out["items"], out["first"], out["room"]) == (600, "tmdb-movie-24", 6 * 300)
    assert out["fields"] == ["kind", "reason", "ref", "title", "year"]
    assert out["reason"] == "Trending this week"
    # The same where those rows were taken out of the grid far above.
    deep = out["deep"]
    assert (deep["items"], deep["first"], deep["room"]) == (600, "tmdb-movie-24", 6 * 300)
    assert deep["shed"] > deep["room"]


def test_coming_back_lays_out_the_kept_titles_under_the_room_of_those_not_kept() -> None:
    out = _run("putBackUnderRoom")
    assert out["room"] == "1800px"
    assert out["top"] == 5100 + 1800


def test_coming_back_lays_out_what_was_kept_with_nothing_asked() -> None:
    out = _run("putBack")
    assert out["laid"] == {"refs": 9, "asked": 0, "pending": 0}
    assert out["pressed"] == "true"
    assert out["focus"] == "tmdb-movie-4"
    assert (out["type"], out["cursor"]) == ("tv", "c9")
    assert out["other"] == 0
    assert out["elsewhere"] == 0


def test_rows_far_above_go_as_room_and_come_back_in_order() -> None:
    out = _run("rowsFarAboveGo")
    assert out["all"] == 624
    assert out["shedWhole"] is True
    assert out["roomIsRows"] is True
    # As much as lies wholly ten screens up, and no more.
    assert out["edge"] is True
    assert (out["backAll"], out["inOrder"], out["room"]) == (True, True, "0px")


def test_back_to_top_shows_two_screens_in_and_goes_to_the_wordmark() -> None:
    out = _run("backToTop")
    assert out["short"] is False
    assert out["deep"] == [True, None]
    assert out["to"]["top"] == 0
    assert out["focus"] == "brand"
