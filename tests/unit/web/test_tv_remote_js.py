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


def test_a_tv_that_starts_after_another_was_lost_is_found_on_looking_again() -> None:
    assert _run("anotherStarts") == {"before": False, "after": True}


def test_a_tv_lost_under_the_open_remote_still_finds_another_that_began_meanwhile() -> None:
    assert _run("beganMeanwhile") == "1/2"


def test_a_play_button_draws_its_ring_or_its_icon_and_never_both() -> None:
    out = _run("waiting")
    assert out["waits"] == [["player-btn-ring"], ["player-btn-ring"]]
    assert out["plays"] == [["ico"], ["ico"]]


def test_what_closes_leaves_with_motion_out_of_reach_and_at_once_under_reduced_motion() -> None:
    out = _run("leaves")
    gone = ["gone"] * 4
    asks = ["gone", "gone", "there", "there"]
    assert out["moves"]["menu"] == [["leaving", "leaving", "gone", "gone"], True, gone, "there"]
    assert out["moves"]["card"] == [
        asks,
        ["gone", "gone", "leaving", "leaving"],
        True,
        asks,
        False,
    ]
    assert out["still"]["menu"][0] == gone
    assert out["still"]["card"][1] == gone


def test_still_watching_draws_the_play_icon_as_what_is_paused_does() -> None:
    assert _run("stillIcon") == ["#i-play", "#i-play"]


def test_a_tv_that_is_gone_keeps_the_options_it_had() -> None:
    before, after, hint = _run("takenOptions")
    assert before == [True, True, True, "480p", "#i-pause"]
    assert after == [True, True, True, "480p", "#i-play"]
    assert "21:08" in hint


def test_at_the_end_the_focus_goes_to_play_again() -> None:
    assert set(_run("endFocus").values()) == {"Play again"}


def test_up_next_stands_over_the_controls_and_says_whether_the_tv_goes_on_by_itself() -> None:
    counted, waits = _run("upnextHint")
    assert "carries on" in counted[0]
    assert "carries on" not in waits[0]
    # in the block of the controls, so that it is anchored to its top; a card that asks is not
    assert counted[1:] == waits[1:] == [True, False]


def test_closed_by_a_mouse_that_gives_no_focus_the_bar_has_it() -> None:
    assert _run("mouseClose") == [True, True]


def test_a_tv_the_person_drops_leaves_the_bar_quietly_and_the_others_stay() -> None:
    out = _run("ownUnpair")
    assert out[:2] == ["Playing on Bedroom TV"] * 2
    assert out[2].startswith("Stopped on Bedroom TV")
    assert out[3:] == [False, 0]


def test_a_takeover_of_an_older_tv_which_stays_listed_is_said() -> None:
    bar, card = _run("olderTaken")
    assert bar.startswith("Stopped on Living room")
    assert card == "Someone else is using Living room"


def test_an_older_tv_gets_no_word_of_pairing_or_of_a_browser() -> None:
    out = _run("olderWords")
    for said in out["older"].values():
        for word in ("pair", "browser", "everywhere", "shows mediaman", "carries on"):
            assert word not in said, said
    assert "paired" in out["page"]["taken"]
    assert "browser" in out["page"]["live"]


def test_the_bar_says_that_mediaman_is_out_of_reach_and_offers_nothing_it_cannot_do() -> None:
    out = _run("barOffline")
    assert out["says"] == ["Playing on Living room", "Can’t reach mediaman"]
    assert out["down"] == []
    assert out["back"] == "Playing on Living room"
    assert out["up"] == ["[data-mini-play]", "[data-mini-back]", "[data-mini-stop]"]


@pytest.mark.parametrize("kind", ["page", "older"])
def test_a_restart_of_mediaman_is_said_with_the_place_and_a_way_to_go_on_from_it(kind: str) -> None:
    out = _run("restarted")
    said = "Your place in Glass Orchard is kept."
    assert out[kind]["card"] == ["mediaman restarted", said]
    assert out[kind]["sent"][0]["rating_key"] == "9001"
    assert 1268 < out[kind]["sent"][0]["position_s"] < 1272
    assert out[kind]["bar"] is False
    assert out[kind]["toasts"] == ["mediaman restarted. " + said]
    assert out["same"] == []
    assert out["both"].startswith("Lost")


def test_a_remote_opened_at_the_end_begins_on_play_again() -> None:
    assert _run("openEnded") == [["Play again"], ["Pause"]]


def test_what_was_announced_of_a_card_goes_with_it() -> None:
    assert _run("statusCleared") == {"said": True, "left": ""}


def test_with_two_tvs_the_bar_goes_to_the_other_and_steers_that_one() -> None:
    out = _run("two")
    assert out["says"] == ["Playing on Living room", "Playing on Bedroom TV"]
    assert out["to"] == "/api/tvs/8/commands"
    assert out["stays"].endswith("Bedroom TV")


def test_a_chromecast_has_the_bar_and_the_remote_of_what_it_can_do_without_a_request() -> None:
    out = _run("cast")
    assert out["bar"] == {"shown": True, "says": "Playing on Den"}
    assert out["title"] == "Glass Orchard · S1 E4"
    assert out["asks"] == 0
    assert out["subtitle"] == "S1 · E4 · The Grafting Knife"
    # all of the remote's controls, and no word that there is no volume
    assert out["shown"] == 11
    assert out["nextName"] == "Next episode, S1 · E5 — Windfall"
    assert out["paused"] == "Paused on Den"
    assert out["byItsOwnRemote"] == "Playing on Den"
    assert (out["at"], out["follows"]) == ("20:20", "21:40")
    assert (out["quality"], out["next"]) == ("720p", "S1 · E5 · The Grafting Knife")
    assert out["did"] == [
        "pause",
        ["seek", 1220],
        ["volume", 0.7],
        ["mute", True],
        ["start", {"subtitle": "11"}],
        ["start", {"quality": "medium"}],
        ["start", {"next": True}],
    ]
    assert out["sent"] == []
    assert out["least"] == "[data-no-volume]"


def test_of_a_series_on_a_chromecast_no_more_is_promised_than_the_episode() -> None:
    out = _run("cast")
    assert out["hint"] == "You can leave this page. Keep mediaman open for the next episode."
    assert out["film"] == "You can leave this page. The TV carries on."


def test_a_press_on_pause_does_not_start_a_chromecast_its_own_remote_has_paused() -> None:
    assert _run("castRaced") == {"did": [], "says": "Paused on Den"}


def test_a_refused_change_on_a_chromecast_is_said_and_leaves_what_plays() -> None:
    out = _run("castRefused")
    assert len(out["toasts"]) == 1
    assert "three screens" in out["toasts"][0]
    assert (out["on"], out["quality"]) == ("Playing on Den", "Auto")


def test_the_next_episode_on_a_chromecast_is_offered_and_the_question_after_three() -> None:
    out = _run("castUpnext")
    assert (out["card"], out["line"]) == ("Next episode in 8 s", "S1 · E5 — Windfall")
    assert out["waits"] == "Next episode"
    assert out["playing"] == "Playing on Den"
    assert out["still"] == ["Are you still watching?", True, True]
    assert out["did"] == ["cancel", ["start", {"next": True}]]


def test_a_chromecast_is_stopped_from_the_bar_and_brought_here_from_the_remote() -> None:
    out = _run("castStop")
    assert out["bar"] is False
    assert out["toasts"] == ["Stopped. Your place is saved at 20:00."]
    assert out["did"] == [["stop"], ["stop"]]
    assert out["href"] == "/watch/play/9001?resume=1"
    assert out["said"] == []
    assert out["unsaved"] == ["Stopped. Your place couldn’t be saved."]


def test_a_stop_that_takes_long_leaves_the_remote_on_what_is_being_stopped() -> None:
    out = _run("castStopSlow")
    assert (out["on"], out["dead"], out["open"]) == ("Playing on Den", True, True)
    assert out["after"] == [False, "Playing on Living room"]
    assert (out["sent"], out["did"], len(out["toasts"])) == ([], ["stop"], 1)


def test_a_lost_chromecast_may_come_back_and_past_that_is_offered_here() -> None:
    out = _run("castLost")
    assert (out["card"], out["dead"]) == ("Den stopped answering", True)
    assert out["acts"] == ["Bring it here", "Stop"]
    assert out["back"] is None
    title, line, acts = out["over"]
    assert title == "Den stopped answering"
    assert "21:08" in line
    assert acts == ["Bring it here", "Close"]
    assert out["hidden"] == {"shown": True, "says": "Lost Den. Tap to see why."}
    assert out["dismissed"] == [False, True]


def test_a_chromecast_that_ended_is_said_and_one_another_tab_follows_is_not() -> None:
    out = _run("castOver")
    shut = {"open": False, "bar": False, "card": None}
    assert out["ended"] == {**shut, "toasts": ["Nothing is playing on Den now."]}
    assert out["stopped"] == {**shut, "toasts": ["Stopped."]}
    assert (out["taken"]["open"], out["taken"]["card"]) == (True, "Den plays something else")
    assert out["elsewhere"] == {**shut, "toasts": []}


def test_a_chromecast_is_found_later_beside_a_tv_and_with_mediaman_out_of_reach() -> None:
    out = _run("castBeside")
    assert out["late"] == [False, {"shown": True, "says": "Playing on Den"}]
    assert out["says"] == ["Playing on Living room", "Playing on Den", "Playing on Den"]
    assert out["after"] == ["Playing on Living room"] * 2 + ["Playing on Den"] * 4
    assert out["did"] == ["pause"]
    assert out["alone"] is False


def test_play_on_in_the_remote_moves_what_plays_to_another_tv_or_here() -> None:
    out = _run("playOn")
    assert out["shown"] == [False, True]
    assert (out["on"], out["at"]) == ("tv-7", 1200)
    assert out["what"] == {
        "quality": "auto",
        "autoplay": True,
        "asked_s": 0,
        "subtitle": "off",
        "rating_key": "9001",
        "position_s": 1200,
    }
    # the TV that gives it up says nothing of it meanwhile
    assert out["between"] == [None, True]
    assert out["moved"] == "Playing on Bedroom TV"
    assert out["failed"] == [False, False]
    assert out["cast"] == ["cast", False]
    assert out["fromCast"] == [["stop"], "Playing on Bedroom TV"]
    assert out["toCast"] == ["Playing on Den", []]
    assert out["unpressed"] == 0
    assert out["failedThenStopped"] == 1
    assert out["besideMove"] == "Playing on Living room"
    assert out["without"] is True
    assert out["here"] == "/watch/play/9001?resume=1"
    here = "/watch/play/9001?resume=1"
    assert out["calledOff"] == [[1, "stop", here], [1, "", here]]
    assert out["said"] == ["Stopped. Your place is saved at 20:00."]
    assert out["upnext"] == ["9002", 0]


@cache
def _called_off() -> dict:
    return _run("calledOff")


START, STOP_NEW, STOP_OLD = "tvs/8/play", "tvs/8/commands stop p8", "tvs/7/commands stop p1"


def test_what_is_called_off_is_stopped_before_the_page_is_left() -> None:
    out = _called_off()
    assert out["stopSent"] == [START, STOP_NEW]
    assert out["unanswered"] == [START, STOP_NEW, STOP_OLD, "go"]


def test_what_the_new_tv_took_before_the_page_heard_is_brought_here_the_same() -> None:
    assert _called_off()["given"] == [START, STOP_NEW, STOP_OLD, "go"]


def test_an_older_tv_that_is_called_off_is_stopped_without_its_next_word() -> None:
    assert _called_off()["begun"] == [[START, STOP_NEW, "go"], "/watch/play/9001?resume=1"]


def test_a_start_refused_as_it_is_called_off_is_waited_for_no_longer_and_not_said() -> None:
    assert _called_off()["refused"] == [[START, STOP_OLD, "go"], []]


def test_a_start_that_is_not_answered_is_waited_for_ten_seconds() -> None:
    before, after = _called_off()["limit"]
    assert before == [START]
    assert after == [START, STOP_OLD, "go"]


def test_a_chromecast_that_is_starting_is_waited_for_once_it_is_chosen() -> None:
    out = _called_off()
    waiting, over = out["cast"]
    assert waiting == []
    assert over == [STOP_OLD, "go"]
    assert out["unchosen"] == [STOP_OLD, "go"]


def test_the_remote_says_it_waits_and_takes_no_second_press() -> None:
    assert _called_off()["waits"] == {
        "did": [START],
        "says": ["Bringing it here", "Stopping the TV that was starting."],
        "ring": True,
        "dead": [True, True],
        "focus": True,
        "bar": "Bringing it here",
        "sheets": 0,
    }


def test_a_titles_button_says_whether_a_tv_plays_it() -> None:
    assert _run("paintsTitles") == [
        [True, "Playing on a TV", False, True],
        [False, "Play on a TV", False, False],
    ]


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


def test_cancel_comes_before_taking_a_tv_from_someone() -> None:
    got = _run("takeOrder")
    assert (got["busy"], got["pair"]) == (["unsay", "take"], ["unsay", "take"])


def test_a_pairing_that_would_take_a_tv_from_someone_starts_on_cancel() -> None:
    assert _run("takeOrder")["focus"] == "unsay"


def test_the_page_says_while_the_bar_and_the_remote_show_and_while_the_bar_goes() -> None:
    # the styles that make room for them read these, not the page itself
    assert _run("pageMarks") == {
        "shown": ["has-tv-mini"],
        "open": ["has-tv-mini", "has-tv-rem"],
        "closing": ["has-tv-mini", "has-tv-rem"],
        "closed": ["has-tv-mini"],
        "stopping": ["has-tv-mini", "is-tv-mini-leaving"],
        "stopped": [],
    }
