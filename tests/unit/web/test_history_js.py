"""history.js run under node against a fake DOM: the list, its search and filters, its rows."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "history_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_an_open_row_plays_its_exit_before_it_folds_and_drops_its_link() -> None:
    out = _run("close")
    assert out["afterClick"] == {"open": True, "closing": True}
    assert "event=5" in out["url"]
    assert out["settled"] == {"open": False, "closing": False}
    assert out["urlAfter"] == "http://mm.test/history?action=kept"


def test_reduced_motion_folds_the_row_at_once_and_slides_nothing() -> None:
    out = _run("reduced")
    assert out["afterClick"] == {"open": False, "closing": False}
    assert out["urlAfter"] == "http://mm.test/history"
    assert out["moves"] == []


def test_opening_a_row_is_left_to_the_browser_and_adds_no_step_to_go_back_over() -> None:
    out = _run("open")
    assert out["prevented"] is False
    assert out["steps"] == ["replace /history?event=5"]


def test_what_is_below_slides_up_with_a_closing_row_and_is_let_go_once_it_has_folded() -> None:
    out = _run("close")
    assert [
        (m["name"], m["prop"], m["to"], m["ms"]) for m in out["moving"] if m["name"] != "card"
    ] == [(n, "transform", "translateY(-300px)", 200) for n in ("second", "more", "said")]
    [card] = [m for m in out["moving"] if m["name"] == "card"]
    assert "300px" in card["to"] and card["prop"] == "clipPath"
    assert not any(m["cancelled"] for m in out["moving"])
    assert all(m["cancelled"] for m in out["moves"])


def test_what_is_below_an_opening_row_slides_down_from_where_it_was() -> None:
    moves = [m for m in _run("open")["moves"] if m["prop"] == "transform"]
    assert {(m["from"], m["to"]) for m in moves} == {("translateY(-300px)", "none")}
    assert "more" in {m["name"] for m in moves}


def test_copy_puts_the_labels_and_values_on_the_clipboard_and_keeps_the_row_open() -> None:
    out = _run("copy")
    assert out["copied"] == [
        "Time: 25 Nov 2026, 01:45:03 (2026-11-24 20:15:03 UTC)\nWho: ana\nIP address: 1.2.3.4"
    ]
    assert out["toasts"] == ["Details copied"]
    assert out["open"] is True


def test_events_are_grouped_by_the_readers_own_days() -> None:
    out = _run("days")
    assert out["days"] == ["Today", "Yesterday", "Thursday 2 January 2025", "Day not known"]
    assert out["sizes"] == [2, 1, 1, 1]


def test_the_next_page_is_asked_for_once_and_lists_no_event_twice() -> None:
    out = _run("more")
    assert out["asked"] == ["/api/history?cursor=c1"]
    assert out["ids"] == ["5", "6", "7", "8"]
    assert "everything" in out["end"]
    assert out["said"] == "2 more events, the last"


def test_a_next_page_that_fails_waits_for_the_button_and_then_goes_on_from_there() -> None:
    out = _run("moreFails")
    assert "did not load" in out["failed"]["text"]
    assert out["failed"]["asked"] == 1
    assert out["again"] == "/api/history?cursor=c1"
    assert out["ids"] == ["5", "6", "7"]
    assert out["focus"] == "7"


def test_a_filter_set_while_more_is_on_its_way_drops_it_and_follows_no_old_cursor() -> None:
    out = _run("filterDuringMore")
    assert out["asked"][:2] == ["/api/history?cursor=c1", "/api/history?action=plays"]
    assert not any("cursor" in url for url in out["asked"][1:])
    assert out["ids"] == ["9"]


def test_typing_asks_once_it_rests_and_a_late_answer_to_older_text_is_dropped() -> None:
    out = _run("typing")
    assert out["before"] == 0
    assert out["once"] == ["/api/history?q=har"]
    assert out["ids"] == ["8"]
    assert out["steps"] == ["push /history?q=har", "replace /history?q=harbour"]


def test_a_filter_is_in_the_address_and_back_brings_the_list_before_it() -> None:
    out = _run("back")
    assert out["url"] == "http://mm.test/history?user=bob"
    assert out["askedFor"] == "/api/history?user=bob"
    assert out["filtered"] == ["8"]
    assert out["askedBack"] == "/api/history"
    assert out["ids"] == ["5", "6"]
    assert out["chips"] == 0


def test_a_person_who_is_no_admin_has_no_filter_by_user() -> None:
    out = _run("viewer")
    assert out["chips"] == ["action", "title", "date"]
    assert "user=" not in out["asked"]


def test_days_are_asked_for_as_the_moments_they_begin_and_end_where_the_reader_is() -> None:
    assert _run("dates") == [
        {"since": "2026-11-24T18:30:00.000Z"},
        {"since": "2026-11-23T18:30:00.000Z", "until": "2026-11-24T18:30:00.000Z"},
        {"since": "2026-11-18T18:30:00.000Z"},
        {"since": "2026-10-31T18:30:00.000Z", "until": "2026-11-03T18:30:00.000Z"},
        None,
        {"until": "2026-11-03T18:30:00.000Z"},
    ]


def test_a_line_says_who_did_what_and_names_no_typed_name_or_address() -> None:
    assert _run("lines") == [
        "ana downloaded Film 1",
        "mediaman deleted Film 1",
        "old (former user) downloaded Film 1",
        "A former user downloaded Film 1",
        "Sign-in failed for an unknown name",
        "Sign-in failed for bob",
        "a subscriber · Label",
        "ana · Label",
        "ana added the user eve (viewer)",
        "ana finished Show",
        "ana started Film",
        "ana watched 2 episodes of Show",
        "ana never heard of Film 1",
    ]


def test_a_play_says_how_far_it_went_or_how_long_it_is() -> None:
    out = _run("plays")
    assert out["subs"] == [
        "Movie · 2023 · stopped at 1 h of 2 h",
        "Movie · 2023 · 2 h",
        "S1E2 · 46 min",
        "S1E2 to E3",
    ]
    assert out["bars"] == ["50%"]


def test_a_persons_own_lines_do_not_name_them() -> None:
    assert _run("ownLine") == ["Downloaded Film 1", "Signed in"]


def test_what_is_searched_for_is_marked_and_the_text_stays_text() -> None:
    out = _run("marks")
    assert out["marks"] == ["Film", "film"]
    assert out["text"].endswith("<b>a Film</b> & film")


def test_with_no_history_there_is_nothing_to_search() -> None:
    out = _run("empty")
    assert "No history yet" in out["text"]
    assert out["tools"] is True


def test_no_match_says_what_was_asked_and_clears_it() -> None:
    out = _run("noMatch")
    assert "“zan”, kept" in out["text"]
    assert out["tools"] is False
    assert out["url"] == "http://mm.test/history"
    assert out["asked"] == "/api/history"


def test_a_failed_list_shows_rows_to_come_then_the_error_and_can_be_tried_again() -> None:
    out = _run("wrong")
    assert out["waiting"] == {"busy": "true", "skel": 8, "rows": 0}
    assert "History did not load" in out["text"]
    assert out["ids"] == ["3"]


def test_a_slow_answer_to_a_filter_keeps_the_list_a_moment_then_shows_rows_to_come() -> None:
    assert _run("slow") == {"soon": 2, "later": 8}


def test_the_count_of_a_filtered_list_says_of_how_many() -> None:
    assert _run("count") == "2 of 1,340 events"


def test_days_the_wrong_way_round_are_told_and_not_asked_for() -> None:
    assert _run("wrongDates") == {"asked": 0, "told": True}


def test_the_titles_to_choose_are_those_listed_and_those_the_server_has() -> None:
    assert _run("titles") == ["Film 5", "Film 6", "Film 70"]


def test_the_newest_are_a_link_away_whenever_the_list_begins_further_down() -> None:
    assert _run("newest") == {
        "deep": "/history",
        "filtered": None,
        "answered": None,
        "back": "/history",
    }


def test_the_newest_under_a_date_are_a_link_away_only_when_the_list_begins_further_down() -> None:
    assert _run("newestOfDates") == {"hidden": None, "deep": "/history?date=week"}


def test_the_moments_an_address_names_stay_a_filter_until_cleared() -> None:
    assert _run("since") == {
        "asked": ["/api/history?per_page=1"],
        "ids": ["5", "6"],
        "chip": "1 Nov – 3 Nov",
        "next": "/api/history?q=x&user=bob&since=2026-11-01&until=2026-11-03",
        "url": "http://mm.test/history?q=x&user=bob&since=2026-11-01&until=2026-11-03",
        "cleared": "/api/history?q=x&user=bob",
        "bad": "Date",
    }


def test_focus_stays_in_the_page_when_what_held_it_goes() -> None:
    assert _run("focus") == {
        "title": "title",
        "user": "user",
        "waiting": True,
        "again": True,
        "loaded": "3",
        "moreWaiting": True,
        "moreAgain": True,
        "moreNone": "3",
    }
