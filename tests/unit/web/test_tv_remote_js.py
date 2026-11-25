"""tv-remote.js run under node on its own template, against a fake TV and a stepped clock."""

from __future__ import annotations

import json
import subprocess
from functools import cache
from pathlib import Path

import jinja2
import pytest

HARNESS = Path(__file__).parent / "js" / "tv_remote_harness.js"
TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"


@cache
def _template() -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    return env.get_template("_tv_remote.html").render()


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario],
        input=_template(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_page_asks_once_and_no_more_while_nothing_plays() -> None:
    assert _run("idle") == {"bar": False, "reads": 1}


def test_the_player_gets_no_bar_and_asks_for_nothing_but_can_open_the_remote() -> None:
    assert _run("player") == {"reads": 0, "open": True, "bar": False}


def test_it_asks_often_with_the_remote_open_less_with_the_bar_and_least_while_hidden() -> None:
    out = _run("polling")
    assert out["bar"] == {"shown": True, "says": "Playing on Living room"}
    assert 11 <= out["barMinute"] <= 12
    assert 55 <= out["openMinute"] <= 61
    assert out["hiddenMinute"] <= 3


def test_a_drag_is_not_moved_by_polling_and_holds_until_the_tv_has_carried_it_out() -> None:
    out = _run("drag")
    assert out["held"] == "36:00"
    assert out["sentWhileHeld"] == 0
    assert out["sent"] == [{"kind": "seek", "position_s": 2160, "play_id": "p1"}]
    assert out["beforeTheTvDid"] == "36:00"
    assert out["after"] == "36:01"
    assert out["other"] == "1:00"


def test_a_drag_begun_on_one_episode_seeks_nothing_on_the_next() -> None:
    assert _run("dragOver") == {"sent": [], "at": "0:00"}


def test_a_drag_the_browser_takes_away_seeks_nothing() -> None:
    assert _run("cancelled") == {"sent": [], "at": "20:00"}


def test_arrows_pressed_in_a_row_are_one_seek() -> None:
    out = _run("arrows")
    assert out["at"] == "20:40"
    assert out["sent"] == [{"kind": "seek", "position_s": 1240, "play_id": "p1"}]


def test_a_dragged_volume_sends_the_newest_once_the_last_is_answered() -> None:
    assert _run("volume")["levels"] == [0.41, 0.7]


def test_a_pause_shows_at_once_and_another_phones_play_is_followed() -> None:
    out = _run("pause")
    assert out["says"] == "Paused on Living room"
    assert out["sent"] == [{"kind": "pause", "play_id": "p1"}]
    assert out["other"] == "Playing on Living room"


def test_a_refused_pause_is_taken_back_and_said() -> None:
    assert _run("refused") == {"says": "Playing on Living room", "toast": 1}


def test_a_pause_the_tv_never_takes_is_not_shown_for_ever() -> None:
    assert _run("unheard") == "Playing on Living room"


def test_a_tv_that_stops_answering_leaves_only_the_way_out_until_it_is_back() -> None:
    out = _run("lost")
    assert out["card"] == "Living room stopped answering"
    assert out["focus"] == "Try again"
    assert out["dead"] is True
    assert out["closeLives"] is True
    assert out["back"] == {"card": None, "dead": False}


def test_a_play_that_ended_with_its_tv_stays_on_the_bar_until_it_is_dismissed() -> None:
    out = _run("lostForGood")
    assert out["bar"] == {"shown": True, "says": "Lost Living room. Tap to see why."}
    assert out["dismissed"] is False


def test_a_dismissed_tv_that_comes_back_is_shown_again() -> None:
    assert _run("dismissed") == {"shown": False, "back": True}


def test_a_tv_someone_else_took_is_said_and_asked_for_no_more() -> None:
    out = _run("taken")
    assert out["card"] == "Someone else is using Living room"
    assert "21:08" in out["hint"]
    assert out["asksOn"] == 0
    assert out["href"] == "/watch/play/9001?resume=1"


def test_a_stop_on_another_device_closes_the_remote_and_says_where_it_stopped() -> None:
    out = _run("stoppedElsewhere")
    assert (out["open"], out["bar"]) == (False, False)
    assert len(out["toasts"]) == 1
    assert "21:08" in out["toasts"][0]


def test_stop_takes_the_bar_away_and_says_so_once() -> None:
    out = _run("stop")
    assert out["bar"] is False
    assert len(out["toasts"]) == 1
    assert out["sent"] == [{"kind": "stop", "play_id": "p1"}]


def test_bringing_it_here_stops_the_tv_first_and_then_opens_the_player() -> None:
    out = _run("bring")
    assert [b["kind"] for b in out["sent"]] == ["stop"]
    assert out["href"] == "/watch/play/9001?resume=1"
    assert out["toasts"] == []


def test_one_failed_request_says_nothing_and_a_lasting_one_does_until_it_is_back() -> None:
    assert _run("offline") == {"once": None, "twice": "This device is offline", "back": None}


def test_nothing_is_asked_for_after_signing_out() -> None:
    assert _run("signedOut") == 0


def test_the_next_episode_is_offered_without_taking_the_focus_and_starts_once() -> None:
    out = _run("upnext")
    assert out["card"].startswith("Next episode in ")
    assert out["line"] == "S1 · E5 · Windfall"
    assert out["focusKept"] is True
    assert out["status"] is True
    assert out["sent"] == [{"kind": "next", "play_id": "p1"}]


def test_still_watching_is_answered_from_the_remote() -> None:
    out = _run("still")
    assert out["card"] == "Are you still watching?"
    assert out["focus"] == "Continue watching"
    assert out["sent"] == [{"kind": "next", "play_id": "p1"}]


def test_the_subtitle_menu_takes_the_focus_gives_it_back_and_sends_the_track() -> None:
    out = _run("subtitles")
    assert out["focus"].startswith("Off")
    assert out["under"] is True
    assert out["escape"] == {"menu": False, "open": True, "focus": "subtitles"}
    assert out["sent"] == [{"kind": "subtitles", "subtitle": "11", "play_id": "p1"}]


def test_back_closes_an_open_menu_then_the_remote_and_the_page_hears_of_neither() -> None:
    out = _run("back")
    assert out["out"] == [
        {"at": 2, "entries": 3, "open": True, "menu": True},
        {"at": 1, "entries": 3, "open": True, "menu": False},
        {"at": 0, "entries": 3, "open": False, "menu": False},
    ]
    assert out["page"] == []
    assert out["mark"] == {"id": "a"}


def test_closing_by_a_button_steps_back_out_of_its_own_entries() -> None:
    out = _run("closedOtherwise")
    # a chosen row, a menu another episode closed, a remote opened again before the step was made
    assert [(t["at"], t["open"], t["menu"]) for t in out["out"]] == [
        (1, True, False),
        (1, True, False),
        (1, True, False),
        (0, False, False),
    ]
    assert out["page"] == []


def test_forward_opens_the_remote_again_and_steps_back_where_nothing_plays() -> None:
    out = _run("forward")
    assert [(t["at"], t["open"]) for t in out["out"]] == [(1, True), (0, False)]
    assert out["page"] == []


def test_the_pages_own_entries_stay_its_own() -> None:
    out = _run("otherEntries")
    assert out["own"] == [0]
    assert out["both"] == [0, 0]
    assert (out["trail"]["at"], out["trail"]["open"]) == (0, False)


def test_a_reload_with_the_remote_open_leads_to_the_page_it_was_opened_on() -> None:
    out = _run("reloaded")
    assert out == {"at": 0, "open": False, "bar": True, "page": []}


def test_closed_opened_and_closed_in_one_breath_it_steps_back_once() -> None:
    assert _run("hasty") == {"at": 1, "open": False}


def test_bringing_it_here_puts_the_player_in_the_place_of_the_remotes_entry() -> None:
    out = _run("bringTrail")
    assert out == {"left": 2, "replaced": True, "href": "/watch/play/9001?resume=1"}


def test_a_tv_that_is_not_answering_stands_still_and_takes_no_presses() -> None:
    assert _run("stands") == {"stands": True, "sent": 0, "inert": True}


def test_a_request_that_never_ends_is_said_stops_the_clock_and_is_asked_again() -> None:
    out = _run("hang")
    assert out == {"card": "mediaman isn’t answering", "stands": True, "back": None}


def test_an_answer_cut_off_in_its_body_takes_nothing_away() -> None:
    assert _run("cutOff") == {"bar": [True, True], "open": True, "card": None}


def test_at_the_end_what_follows_is_offered_and_never_the_last_second_again() -> None:
    out = _run("atTheEnd")
    on = [{"rating_key": "9002", "position_s": 0}]
    for case, label in (
        ("still", "Continue watching"),
        ("lost", "Play next episode"),
        ("unseen", "Play next episode"),
    ):
        assert out[case]["labels"] == [label, "Bring it here", "Close"], case
        assert out[case]["sent"] == on, case
        assert out[case]["href"] == "/watch/play/9002?resume=1", case
    for case in ("none", "unknown"):
        assert out[case] == {"labels": ["Close"], "sent": [], "open": False}, case


def test_a_play_lost_midway_goes_on_from_its_place() -> None:
    assert _run("midway") == [{"rating_key": "9001", "position_s": 1268}]


def test_a_lost_tv_leaves_the_way_to_another_that_plays() -> None:
    out = _run("lostOfTwo")
    assert out["says"][0].startswith("Stopped on Living room")
    assert out["other"] == "1/2"
    assert out["asks"] == 3
    assert out["says"][1:] == ["Playing on Bedroom TV", "Playing on Bedroom TV"]


def test_a_dismissed_tv_gives_the_bar_to_another_that_plays() -> None:
    out = _run("dismissedOfTwo")
    assert out["live"] == "Playing on Bedroom TV"
    assert out["ended"] == {"shown": True, "says": "Playing on Bedroom TV"}


def test_a_tv_that_starts_after_another_was_lost_is_found_when_the_page_is_looked_at_again() -> None:
    assert _run("anotherStarts") == {"before": False, "after": True}


def test_a_tv_lost_under_the_open_remote_still_finds_another_that_began_meanwhile() -> None:
    assert _run("beganMeanwhile") == "1/2"


def test_with_two_tvs_the_bar_goes_to_the_other_and_steers_that_one() -> None:
    out = _run("two")
    assert out["says"] == ["Playing on Living room", "Playing on Bedroom TV"]
    assert out["to"] == "/api/tvs/8/commands"
    assert out["stays"].endswith("Bedroom TV")


def test_a_tv_that_cannot_set_its_volume_gets_words_and_no_slider() -> None:
    assert _run("noVolume") == {"slider": False, "words": True}


def test_the_remote_opens_on_a_tv_given_whole_or_by_id() -> None:
    out = _run("opens")
    assert out["given"] == {"open": True, "on": "Starting on Playroom"}
    assert out["byId"] is True


@pytest.mark.parametrize("node_id", ["tv-mini", "tv-remote"])
def test_both_are_hidden_until_something_plays(node_id: str) -> None:
    tag = _template().split(f'id="{node_id}"')[1].split(">")[0]
    assert tag.endswith(" hidden")
