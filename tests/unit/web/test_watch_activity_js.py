"""watch/activity.js runs with the real watch/space.js under node against a fake DOM: the Storage
panel's Activity tab, its periods, its graphs grown and read, and its motion."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from tests.helpers.node_batch import run_node

HARNESS = Path(__file__).parent / "js" / "watch_activity_harness.js"


@cache
def _run(scenario: str):
    result = run_node(HARNESS, scenario)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_tab_asks_every_two_seconds_while_on_show_and_stops_when_left():
    out = _run("polls")
    assert (out["before"], out["opened"], out["ticked"]) == (0, 1, 2)
    assert out["away"] == 2
    assert out["foot"] == {"act": True, "scan": False}  # Scan now's foot is back on Storage


def test_a_period_asks_for_its_history_and_says_which_it_is():
    out = _run("period")
    assert out["asked"] == ["/api/activity/history?period=24h"]
    assert out["checked"] == ["24h"]
    assert out["note"] == "the last 24 hours"
    assert (
        out["max"] == "250\u00a0MB/s"
    )  # the scale fits the period's 200 MB/s, not the minute's 100


def test_the_periods_move_by_arrow_key_and_wrap_round():
    assert _run("periodKeys") == {"on": "30d", "focused": "30d"}


def test_enter_grows_a_graph_over_the_panel_and_escape_puts_it_back_before_closing_anything():
    out = _run("grow")
    assert out["grown"]["layer"] and out["grown"]["title"] == "Network"
    assert out["grown"]["held"] == [True, True, True]
    assert "act-chart--big" in out["grown"]["focused"]
    assert out["after"] == {"layer": False, "held": False, "open": True, "focused": True}


def test_the_crosshair_reads_the_values_under_the_pointer_and_goes_when_it_leaves():
    out = _run("crosshair")
    assert out["early"]["tip"] is False
    assert "10\u00a0MB/s" in out["early"]["text"]
    assert "200\u00a0MB/s" in out["late"]
    assert out["gone"] is True


def test_a_source_that_does_not_answer_is_said_in_words():
    out = _run("down")
    assert out["warn"] is True
    assert out["say"].startswith("NZBGet isn’t answering")
    assert "NZBGet isn’t answering" in out["row"]
    assert "Docker proxy" in out["quiet"]


def test_with_reduced_motion_numbers_change_at_once_and_nothing_slides_in():
    out = _run("reduced")
    assert out == {"arriving": False, "frames": 0, "down": "300\u00a0MB/s"}


def test_with_motion_numbers_count_and_the_sections_come_in():
    out = _run("motion")
    assert out["arriving"] is True
    assert out["frames"] > 0
    assert out["down"] == "300\u00a0MB/s"  # where the count ends


def test_a_graph_is_broken_where_the_history_has_a_gap():
    assert _run("slots") == [0, 10, 10, 10, None, None, None, None, None, None, 5]


def test_an_answer_on_its_way_when_the_tab_is_hidden_does_not_ask_again():
    assert _run("hidden") == {"asked": 0}


def test_the_crosshair_reads_the_slot_under_the_pointer_and_its_tip_turns_back_at_the_right():
    out = _run("slot")
    assert "140 MB/s" in out["mid"]["text"]  # slot 79, part way from 10 to 200 MB/s
    assert out["mid"]["hair"] == "translateX(149.1px)"
    assert out["left"] == "translateX(12.0px)"  # beside the pointer
    assert out["right"] == "translateX(208.0px)"  # 300 px less 12 and the tip's 80


def test_before_the_first_point_a_graph_holds_its_value_and_never_goes_below_zero():
    assert _run("edge") == [10, 10, 100]


def test_a_24_hour_tooltip_says_the_day_of_a_point_that_is_not_today():
    out = _run("day")
    assert out["before"] == "Fri 27 Nov, 12:45"  # en-GB in UTC, as the harness writes dates
    assert out["today"] == "12:00"


def test_a_frame_that_began_before_the_count_shows_no_value_beyond_either_end():
    shown = _run("early")
    assert set(shown) <= {"100 MB/s", "300 MB/s"}


def test_with_the_servers_size_set_memory_is_in_gb_and_free_and_the_rest_add_up():
    out = _run("sized")
    assert out["note"] == "Server · 10 cores · 16 GB"
    assert out["tile"] == "of 16 GB"
    assert dict(out["keys"]) == {
        "Plex": "1.0 GB",  # 6.25% of 16 GB
        "Sonarr": "600 MB",
        "mediaman": "140 MB",
        "Everything else": "6.3 GB",  # 8 GB in use, less what the apps use
        "Free": "8.0 GB",
    }
    assert out["ticks"][-1] == "0 GB"


def test_without_it_the_server_and_plex_are_shares_and_nothing_is_turned_into_gb():
    out = _run("shares")
    assert out["note"] == ""
    assert out["tiles"][1:] == ["of the server", "of the server"]
    assert out["fig"] == "50%"
    assert out["keys"] == [["Plex", "6%"], ["Everything else", "44%"], ["Free", "50%"]]


def test_a_download_being_unpacked_says_which_app_takes_it_and_is_not_drawn_as_busy():
    out = _run("unpacking")
    assert out["sub"].endswith("Sonarr imports it next")
    assert out["cls"] == "act-sub__val"
