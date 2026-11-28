"""tv-remote.js run under node on its own template, against a fake TV and a stepped clock."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import jinja2
import pytest

from tests.helpers.node_batch import run_node

HARNESS = Path(__file__).parent / "js" / "tv_remote_harness.js"
TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"


@cache
def _template() -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    return env.get_template("_tv_remote.html").render()


def _run(scenario: str, *args: str):
    result = run_node(HARNESS, scenario, *args, input=_template())
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


def test_the_remotes_back_never_says_the_tv_keeps_playing_while_it_is_paused() -> None:
    assert "playing" not in _run("pause")["back"]


def test_anothers_film_is_named_as_theirs_and_their_pause_as_by_them() -> None:
    out = _run("anothers")
    assert out["says"] == "maren’s · on Living room"
    assert out["bring"] is True and "maren’s Continue watching" in out["hint"]
    assert out["paused"] == "Paused by maren"


def test_the_remote_says_who_paused_anothers_film_and_whose_place_its_stop_kept() -> None:
    out = _run("anothers")
    assert out["on"] == "Paused by maren on Living room"
    assert out["toasts"] == ["Stopped. maren’s place is saved at 20:00."]


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


def test_the_bar_says_once_who_else_stopped_your_film_and_that_your_place_is_saved() -> None:
    out = _run("stoppedByAnother")
    assert out["maren"] == {
        "bar": False,
        "toasts": ["maren stopped Glass Orchard on Living room. Your place is saved at 21:08."],
    }
    assert out["nobody"] == {"bar": False, "toasts": []}


def test_another_stopping_their_own_film_is_said_as_theirs_on_bar_and_remote() -> None:
    out = _run("stopsTheirOwn")
    said = ["maren stopped Glass Orchard. Their place is kept."]
    assert out["bar"] == {"open": False, "bar": False, "toasts": said}
    assert out["remote"] == {"open": False, "bar": False, "toasts": said}


def test_an_unpaired_person_is_told_who_unpaired_them_and_what_stopped_and_both_go_with_motion() -> (
    None
):
    out = _run("unpaired")
    removed = ["maren unpaired you from Living room. Glass Orchard stopped; your place is saved."]
    bar = {"leaving": True, "hidden": True, "toasts": removed, "over": [False], "asksOn": 0}
    assert out["removed"] == bar
    # Said once the remote has gone, so that it does not move from under its title to the foot.
    assert out["removedOpen"] == bar
    assert out["left"]["toasts"] == ["You left Living room."]


def test_a_film_started_over_yours_names_who_started_it_and_keeps_your_place() -> None:
    out = _run("takenOver")
    said = ["maren started Dune on Living room. Your place in Glass Orchard is kept."]
    assert out["bar"]["toasts"] == said
    # The hint shows only while it loads: the toast stays long enough to be read.
    assert out["remote"] == {
        "toasts": said,
        "over": [True],
        "hint": "maren started Dune. Your place in Glass Orchard is kept.",
    }
    assert out["loaded"] == []


def test_play_on_too_tall_for_the_room_by_its_button_takes_the_screens_height_instead() -> None:
    out = _run("sheetHeight")
    # The sheet's own rule then sizes it to the screen, inside its safe areas.
    assert out["360"] == {"full": True, "bottom": "", "maxHeight": ""}
    assert out["200"] == {"full": False, "bottom": "68px", "maxHeight": "318px"}


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


def test_a_poll_that_finds_the_session_gone_asks_before_leaving_unsaved_edits() -> None:
    out = _run("unsavedEdits")
    password = "You must change your password before continuing."
    assert out["redirect"] == {"href": "", "asked": [password], "asksOn": 0}
    assert out["signedOut"] == {"href": "", "asked": ["Your session has expired."], "asksOn": 0}
    assert out["signedOutLeave"]["href"].startswith("/login")
    assert out["signedOutSaved"]["href"].startswith("/login")
    assert out["signedOutSaved"]["asked"] == []


def test_on_the_page_a_read_is_sent_to_the_remote_never_reloads_it_and_stops_asking() -> None:
    out = _run("onTheWayPage")
    assert out["/force-password-change"] == {"went": "", "reads": 1}
    assert out["/login"] == {"went": "", "reads": 1}


def test_opening_a_tv_while_the_phone_is_offline_blames_the_connection_not_the_tv() -> None:
    (toast,) = _run("phoneOffline")["toasts"]
    assert "TV" not in toast
    assert "connection" in toast


def test_nothing_is_asked_for_after_signing_out() -> None:
    assert _run("signedOut") == 0


def test_the_next_episode_is_offered_without_taking_the_focus_and_starts_once() -> None:
    out = _run("upnext")
    assert out["card"].startswith("Next episode in ")
    assert out["card"].endswith(" · Press to cancel")
    # Cancel is first and the main one: to watch on, the person need only wait.
    assert out["acts"] == [["Cancel", True], ["Play now", False]]
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
    # The seek row, the middle, play, quality and next: none is reached under the chooser.
    assert out["under"] == [True] * 5
    assert out["polled"] == {"reads": True, "under": [True] * 5}
    assert out["after"] == [False] * 5
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


def test_a_takeover_of_a_tv_which_stays_listed_is_said() -> None:
    bar, card = _run("listedTaken")
    assert bar.startswith("Stopped on Living room")
    assert card == "Someone else is using Living room"


def test_a_paired_tv_says_why_it_stopped_in_words_of_pairing_and_of_its_browser() -> None:
    assert _run("pairedWords") == {
        "taken": "Someone else is using Living room. They paired it, and yours stopped at 20:00.",
        "unpaired": "Living room was unpaired. It was unpaired in Settings, or in your Profile, "
        "or on the TV. To use it again, pair it.",
        "signed_out": "Living room stopped. You signed out everywhere, which ends what plays on a TV.",
        "lost": "Living room stopped answering. It was off, asleep or away from its browser "
        "for 2 minutes, so it stopped at 20:00.",
        "live": "It may be off, asleep or away from its browser. If it comes back within 2 minutes, "
        "it carries on by itself.",
        "off": "! The TV isn’t answering. Check it still shows mediaman.",
    }


def test_the_bar_says_that_mediaman_is_out_of_reach_and_offers_nothing_it_cannot_do() -> None:
    out = _run("barOffline")
    assert out["says"] == ["Playing on Living room", "Can’t reach mediaman"]
    assert out["down"] == []
    assert out["back"] == "Playing on Living room"
    assert out["up"] == ["[data-mini-play]", "[data-mini-back]", "[data-mini-stop]"]


def test_a_restart_of_mediaman_is_said_with_the_place_and_a_way_to_go_on_from_it() -> None:
    out = _run("restarted")
    said = "Your place in Glass Orchard is kept."
    assert out["card"] == ["mediaman restarted", said]
    assert out["sent"][0]["rating_key"] == "9001"
    assert 1268 < out["sent"][0]["position_s"] < 1272
    assert out["bar"] is False
    assert out["toasts"] == ["mediaman restarted. " + said]
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
    assert (out["card"], out["line"]) == (
        "Next episode in 8 s · Press to cancel",
        "S1 · E5 — Windfall",
    )
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
    # Moved with no subtitle showing, the server picks the person's own again: "off" would be
    # kept on to the next episodes and hide their signs and songs.
    assert out["subtitleShown"] == "11"


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


def test_playing_over_a_film_of_someone_the_tv_is_shared_with_asks_by_name() -> None:
    out = _run("takeNamed")
    assert "maren is watching Dune" in out["said"] and "Their place is kept" in out["said"]
    assert (out["take"], out["primary"]) == ("Play mine instead", True)


def test_a_phone_sent_on_to_a_page_closes_the_remote_with_motion_and_goes_there() -> None:
    out = _run("sentAway")
    away = {
        "leaving": True,
        "before": "",
        "href": "/force-password-change",
        "hidden": True,
        "toasts": [],
        "asksOn": 0,
    }
    assert out["redirect"] == away
    assert out["redirectStill"] == away
    assert out["refused"] == away
    assert out["signedOut"] == {**away, "href": "/login?next=%2Fwatch%3Fq%3D1"}
    # A path that would read as another host is not kept for after signing in.
    assert out["signedOutOddPath"]["href"] == "/login"


def test_nothing_playing_said_while_the_remote_closes_waits_until_it_has_gone() -> None:
    out = _run("reopenedClosing")
    said = ["Nothing is playing on Living room now."]
    assert out["moves"] == {"early": 0, "toasts": said, "over": [False]}
    assert out["still"] == {"early": 0, "toasts": said, "over": [False]}


def test_audio_picked_on_the_remote_is_kept_and_the_tv_starts_again_with_it() -> None:
    out = _run("tracksTv")
    assert out["label"] == "English"
    assert out["foot"].startswith("Changes play on Living room.")
    assert out["kept"] == [["PUT", "/api/watch/tracks/9001", {"audio": "62"}]]
    # Not subtitles "off", which would go on to the next episodes and hide their signs and songs.
    assert out["sent"] == [{"kind": "subtitles", "subtitle": None, "play_id": "p1"}]
    assert out["toasts"] == ["Deutsch audio on Living room."]


def test_audio_picked_while_subtitles_show_names_the_subtitles_that_go_on() -> None:
    out = _run("tracksKeepSubtitle")
    assert out["sent"] == [{"kind": "subtitles", "subtitle": "11", "play_id": "p1"}]


def test_audio_picked_while_subtitles_show_that_then_do_not_clears_the_tvs_own_choice() -> None:
    out = _run("tracksDropSubtitle")
    assert out["sent"] == [{"kind": "subtitles", "subtitle": None, "play_id": "p1"}]


def test_audio_picked_for_a_chromecast_is_asked_of_its_next_stream() -> None:
    out = _run("tracksCast")
    assert out["did"] == [["start", {"subtitle": None, "audio": "62"}]]
    assert out["toasts"] == ["Deutsch audio on Den."]


def test_audio_the_tv_cannot_play_is_taken_back() -> None:
    out = _run("tracksRefused")["track_unavailable"]
    assert out["kept"][-1] == ["DELETE", "/api/watch/tracks/9001", None]
    assert "! Couldn’t change the audio. Staying on English." in out["toasts"]


def test_audio_kept_when_the_tv_fails_for_another_reason_stays_kept() -> None:
    out = _run("tracksRefused")["tv_off"]
    assert out["kept"] == [["PUT", "/api/watch/tracks/9001", {"audio": "62"}]]
    assert out["toasts"] == ["! The TV isn’t answering. Check it still shows mediaman."]


def test_subtitles_the_tv_cannot_play_put_back_what_was_kept_and_never_a_new_off() -> None:
    out = _run("tracksRefused")
    pick = ["PUT", {"subtitle": "11"}]
    choice = {
        "rating_key": "8001",
        "subtitle": None,
        "subtitle_id": None,
        "subtitle_lang": None,
        "subtitle_forced": False,
        "subtitle_hearing_impaired": False,
    }
    # What was kept goes back as the server gave it: Off only where Off was kept, and a kept
    # track of which nothing shows here is not lost.
    assert out["audioKept"] == [pick, ["PUT", {"subtitle_choice": choice}]]
    assert out["offKept"] == [pick, ["PUT", {"subtitle_choice": {**choice, "subtitle": "off"}}]]
    on = {**choice, "subtitle": "on", "subtitle_id": "5", "subtitle_lang": "en"}
    on["subtitle_hearing_impaired"] = True
    assert out["onKept"] == [pick, ["PUT", {"subtitle_choice": on}]]
    # Not known: nothing is named for the subtitles, so none is made Off.
    assert out["unknown"] == [pick, ["PUT", {"subtitle": None}]]


def test_a_choice_kept_for_the_title_can_go_back_to_the_defaults_from_the_remote() -> None:
    out = _run("tracksDefaults")
    for on in ("tv", "cast"):
        assert "Your default" in out[on]["tag"]
        assert out[on]["foot"].endswith("Use my defaults")
        assert out[on]["kept"] == [["DELETE", "/api/watch/tracks/9001"]]
    assert out["tv"]["sent"] == [{"kind": "subtitles", "subtitle": None, "play_id": "p1"}]
    assert out["cast"]["did"] == [["start", {"subtitle": None, "audio": "61"}]]
    assert out["tv"]["toasts"][-1] == "Back to your defaults: English."


def test_defaults_the_tv_cannot_play_put_back_the_choice_kept_before() -> None:
    choice = {
        "rating_key": "9001",
        "subtitle": "on",
        "subtitle_id": "11",
        "subtitle_lang": "en",
        "subtitle_forced": False,
        "subtitle_hearing_impaired": False,
    }
    assert _run("tracksDefaultsRefused") == [
        ["DELETE", None],
        ["PUT", {"audio": "62", "subtitle_choice": choice}],
    ]


def test_signs_and_songs_are_capitalised_only_where_a_sentence_begins() -> None:
    assert _run("saidSigns") == [
        "Back to your defaults: English, signs & songs only.",
        "Signs & songs only. Kept for Glass Orchard.",
    ]


def test_a_file_plex_cannot_play_is_said_with_no_try_again_unlike_an_outage() -> None:
    out = _run("failed")
    assert "Dolby Vision Profile 5" in out["unplayable_dolby_vision_5"]["line"]
    assert "Try again" not in out["unplayable_dolby_vision_5"]["labels"]
    assert "Try again" in out["unreachable"]["labels"]


def test_a_tv_that_cannot_start_a_file_plex_cannot_play_offers_no_try_again() -> None:
    out = _run("sheetRefused")
    assert "Dolby Vision Profile 5" in out["unplayable_dolby_vision_5"]
    assert "Try again" not in out["unplayable_dolby_vision_5"]
    assert "Try again" in out["unreachable"]


# The bar's box and the card's, as the harness lays them out.
# The bar on a laptop (left 340, top 822, 760 x 62) as it stands in the card (450, 214, 540 x 670).
ON_BAR = {"top": "608px", "left": "-110px", "width": "760px", "height": "62px"}


def test_the_card_grows_out_of_the_bar_its_glass_alone_laid_out_as_it_goes() -> None:
    out = _run("grows")
    assert out["from"] == ON_BAR
    assert out["to"] == {"top": "0px", "left": "0px", "width": "540px", "height": "670px"}
    # The picture, title and line travel from the bar's; the bar itself fades as the card grows.
    for part in ("data-morph-art", "data-title", "player-seek-track", "data-veil"):
        assert part in out["moved"], part
    assert out["bar"] == [["1", "0"]]
    # The glass holds nothing, so its box can move; all else moves by transform and opacity alone.
    assert out["glass"] == ["height", "left", "opacity", "top", "width"]
    assert out["others"] == ["opacity", "transform"]


def test_a_closed_card_shrinks_into_the_bar_and_is_hidden_only_once_it_has() -> None:
    assert _run("shrinks") == {
        "to": ON_BAR,
        "shownMeanwhile": True,
        "focus": True,
        "hiddenAfter": True,
        "left": 0,
    }


def test_a_card_closed_as_it_opens_turns_round_from_where_it_is() -> None:
    assert _run("turnsRound") == {"from": "300px", "to": ON_BAR["top"]}


def test_with_less_motion_the_card_only_fades() -> None:
    out = _run("growsCalm")
    assert out["props"] == ["opacity"]
    assert {"data-veil", "data-glass"} <= set(out["moved"])
    assert "data-morph-art" not in out["moved"]


def test_on_the_player_with_no_bar_the_card_rises_from_the_foot() -> None:
    out = _run("rises")
    assert out["from"] == ["translateY(48px)"]
    # Its glass fades on its own, never under a fading sheet, where it would blur nothing.
    assert {"data-veil", "data-sheet", "data-glass", "data-hero"} <= set(out["moved"])


def test_a_tap_beside_the_card_closes_it_and_gives_the_bar_the_focus() -> None:
    assert _run("veil") == {"open": False, "focus": True}


def test_a_sheet_pulled_down_far_closes_and_a_short_pull_springs_back() -> None:
    out = _run("pulled")
    assert out["40"] == {"held": "translateY(40px)", "open": True, "from": ["translateY(40px)"]}
    assert out["160"]["open"] is False
    assert out["160"]["from"] == ["translateY(160px)"]
    # A mouse does not pull.
    assert out["mouse"] == {"held": "", "open": True, "from": []}


def test_a_press_begun_before_the_card_settled_stops_nothing_and_a_later_one_does() -> None:
    assert _run("doublePress") == {
        "at600": [],
        "late": ["stop"],
        "at1s": ["stop"],
        "keyed": ["stop"],
    }


def test_as_the_card_grows_a_tap_beside_it_closes_it_but_not_one_where_the_bar_stood() -> None:
    out = _run("tapBeside")
    assert out["onBar"] == {"growing": True, "open": True, "grown": True}
    assert out["beside"]["open"] is False


def test_a_card_closed_as_it_opens_is_measured_with_the_opening_stopped() -> None:
    assert _run("measuredStopped")[-1] == 0


def test_the_bar_leans_under_a_mouse_and_back_but_not_under_a_finger_nor_with_less_motion() -> None:
    out = _run("leans")
    # The glass swells; the words on it are drawn back to their size.
    assert out["mouse"] == {
        "to": ["scale(1.016, 1.08)"],
        "words": ["scale(0.984, 0.926)"],
        "back": ["none"],
    }
    for still in ("finger", "still", "noHover"):
        assert out[still] == {"to": [], "words": [], "back": []}, still


def test_opened_from_the_lean_the_bar_is_held_there_until_the_card_is_back_in_it() -> None:
    assert _run("heldBar") == {
        "held": "matrix(0.5, 0, 0, 0.5, 10, 20)",
        "still": "none",
        "leaning": 0,
        "closing": "matrix(0.5, 0, 0, 0.5, 10, 20)",
        "after": "",
    }


def test_the_glass_is_drawn_before_the_bar_goes_and_one_title_is_always_there() -> None:
    out = _run("spans")
    opening, closing = out["open"], out["close"]
    # Opening: the glass is whole before the bar starts to fade, and the bar's title goes along with
    # the card's as the card's comes, so the one fades into the other in one place.
    assert opening["glass"][1] <= opening["bar"][0]
    assert opening["barTravels"] == opening["travels"]
    assert opening["title"][0] == 0 and opening["title"][1] <= opening["bar"][1]
    # Closing: all is home, or nearly, before the bar comes back; the bar is back before the glass
    # goes, and the card's title goes as the bar's comes, so a title is always there.
    assert closing["barTravels"] == closing["travels"]
    assert closing["bar"][0] < closing["travels"][1] <= closing["glass"][1]
    assert closing["bar"][0] < closing["glass"][0]
    assert closing["bar"][0] < closing["title"][1] and closing["title"][0] < closing["bar"][1]
    film = _run("spans", "film")["close"]
    assert film["bar"][0] < film["title"][1] and film["title"][0] < film["bar"][1]


def test_the_rest_comes_in_row_by_row_once_the_glass_is_most_of_the_way_and_goes_first() -> None:
    out = _run("rows")
    opening, closing = out["open"], out["close"]
    starts = [row[0] for row in opening["rows"]]
    # Top to bottom, each after the last, none before a quarter of the glass's own growing.
    assert starts == sorted(set(starts))
    assert starts[0] >= opening["grows"][1] / 4
    # Each comes in to how the page draws it (here half-faint), never to full and then back.
    assert opening["drawn"] == "0.5"
    # Closing, every row is gone within the first eighth of the glass's going, long before it fades.
    assert all(row[1] <= closing["grows"][1] / 8 for row in closing["rows"])
    assert max(row[1] for row in closing["rows"]) < closing["glass"][0]


def test_a_click_with_no_press_before_it_acts_once_the_card_has_settled() -> None:
    assert _run("unpressed") == ["stop"]


def test_a_title_too_long_for_the_bar_s_room_does_not_travel_into_it() -> None:
    out = _run("longTitle")
    assert any(t.startswith("translate(") and "scale(" in t for t in out["short"])
    assert out["long"] == ["translateY(14px)"]


def test_closed_with_the_mouse_still_on_the_bar_the_bar_swells_again() -> None:
    out = _run("rearmed")
    assert out["still"] == ["scale(1.016, 1.08)"]
    assert out["gone"] == []


def test_a_card_that_follows_the_next_episode_leaves_no_old_countdown_in_the_status() -> None:
    out = _run("statusFollows")
    assert out["still"]["card"] == "Are you still watching?"
    assert out["failed"]["card"].startswith("This can’t play on")
    assert "Next episode" not in out["still"]["status"]
    assert "Next episode" not in out["failed"]["status"]


def test_the_remote_s_title_can_be_read_whole_where_it_is_cut_short() -> None:
    assert _run("titleWhole") == "A Very Long Title That Runs On and On Past Two Lines of the Card"


def test_a_chromecast_in_an_intro_is_offered_skip_and_its_own_count_can_be_cancelled():
    out = _run("castSkip")
    assert out["offered"] == "Skip intro"
    assert out["pressed"] == [["seek", 150]]
    assert out["counting"] == ["Skipping intro in 4 · Cancel", True]
    assert out["kept"] == ["Skip intro", ["keepSkip"]]
