"""Playing on a Chromecast, run under node against a stand-in for Google's script and a TV: when
Google's script is asked for, what the TV is sent, what the player is told, and what keeps a
title's place while no player follows it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

HARNESS = Path(__file__).parent / "js" / "watch_cast_sender_harness.js"
SDK = "https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1"


def _run(*scenario: str) -> Any:
    result = subprocess.run(
        ["node", str(HARNESS), *scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_googles_script_is_asked_for_once_and_only_when_play_on_is_opened() -> None:
    out = _run("quiet")
    assert out["before"] == {"scripts": [], "there": False}
    assert (out["scripts"], out["there"], out["changes"]) == ([SDK], True, True)
    assert out["options"] == {
        "receiverApplicationId": "CC1AD845",
        "autoJoinPolicy": "origin_scoped",
    }


@pytest.mark.parametrize("browser", ["notChrome", "plainHttp", "noSheet"])
def test_a_browser_that_cannot_cast_is_offered_nothing_and_loads_nothing(browser: str) -> None:
    assert _run("unable")[browser] == {"ways": 0, "scripts": []}


@pytest.mark.parametrize("how", ["blocked", "refuses", "silent"])
def test_a_script_that_does_not_come_says_so_where_it_is_pressed_and_is_got_over(how: str) -> None:
    out = _run("fails")[how]
    assert out["before"] == {"looking": True, "trouble": None}
    assert (out["told"], out["there"], out["looking"], out["asks"]) == (True, False, False, False)
    assert out["trouble"] == "Chromecast isn’t answering"
    assert (out["start"], out["open"]) == ({"code": "failed", "says": True}, 0)
    assert out["after"] == {"there": True, "trouble": None, "asks": True}


@pytest.mark.parametrize("how", ["blocked", "refuses"])
def test_a_script_that_failed_is_asked_for_again_and_google_is_not_told_the_page(how: str) -> None:
    assert _run("retries")[how] == {
        "there": True,
        "asked": 2,
        "policies": ["no-referrer", "no-referrer"],
    }


def test_a_slow_script_is_used_when_it_comes_and_never_loaded_twice() -> None:
    assert _run("slowScript") == {
        "gaveUp": True,
        "setUps": 1,
        "tags": 1,
        "then": True,
        "after": 1,
        "trouble": None,
        # One that is there already is not asked for.
        "there": True,
        "asked": [],
    }


def test_the_sheet_knows_whether_there_is_a_tv_or_chrome_still_searches() -> None:
    out = _run("fails")
    assert out["noTv"] == {"there": False, "looking": True}
    assert out["searched"] == {"there": False, "looking": False}
    assert out["found"] == {"there": True, "looking": False}


def test_a_script_that_throws_is_a_tv_that_is_not_there_and_breaks_nothing_else() -> None:
    out = _run("throws")
    assert out["way"] == {"there": False, "looking": True, "asks": False}
    assert (out["asks"], out["name"]) == (True, "")
    assert out["now"] == ["", False, 1, False]


def test_what_one_account_plays_is_not_there_for_another_in_the_same_browser() -> None:
    out = _run("account")
    assert out["seen"] == [[None, None, False, None]] * 2
    assert (out["scripts"], out["posts"], out["ended"]) == ([0, 0, 0], [], [])
    # It is left as it was for the first.
    assert out["same"] is True
    assert out["kept"] == {"played_s": 10}


def test_a_page_that_does_not_know_whose_it_is_plays_and_stores_nothing() -> None:
    assert _run("unmarked") == {"plays": 1, "stored": []}


@pytest.mark.parametrize(("page", "gone"), [("player", ["stopped"]), ("keeper", [])])
def test_signing_out_forgets_it_and_the_tv_and_its_stream_are_left_to_themselves(
    page: str, gone: list[str]
) -> None:
    assert _run("signIn")[f"{page} out"] == {
        "stored": 0,
        "gone": gone,
        "ended": [],
        "posts": [],
        "now": False,
    }


@pytest.mark.parametrize("page", ["player", "keeper"])
def test_the_sign_in_page_opened_by_somebody_signed_in_changes_nothing(page: str) -> None:
    out = _run("signIn")[f"{page} in"]
    assert (out["stored"], out["gone"], out["ended"], out["posts"]) == (2, [], [], [])
    assert out["now"] is (page == "keeper")


def test_a_session_that_plays_nothing_of_this_pages_is_not_used_unasked() -> None:
    out = _run("joined")
    assert (out["pickers"], out["second"], out["third"]) == (1, 1, 2)
    assert out["asks"] == [True, False, False, True]


def test_a_tv_that_is_not_found_is_looked_for_a_few_times_and_then_only_when_asked() -> None:
    out = _run("misses")
    assert out["loads"] == [1, 1, 0, 0, 0]
    assert (out["kept"], out["found"], out["again"]) == (True, True, 1)


def test_the_tv_gets_one_streams_address_its_words_and_its_place_and_nothing_else() -> None:
    out = _run("starts")
    assert out["load"] == {
        "url": "https://media.example/api/cast/ticket1/index.m3u8",
        "type": "application/vnd.apple.mpegurl",
        "at": 100,
        "autoplay": True,
        "title": "Glass Orchard",
        "line": "S1 · E4",
        "keys": ["autoplay", "currentTime", "media"],
        "info": ["contentId", "contentType", "customData", "duration", "metadata", "streamType"],
        # A picture is behind the session, which the TV does not have.
        "images": None,
    }
    assert (out["name"], out["carriesOn"], out["ready"]) == ("Living room", True, 4)
    assert out["events"] == ["canplay", "playing"]


def test_the_ticket_is_never_stored_in_the_browser() -> None:
    stored = _run("starts")["stored"]
    assert "st1" in stored
    assert "ticket" not in stored


def test_a_tv_that_gives_back_only_the_address_is_known_by_it() -> None:
    assert _run("starts", "forgetful")["events"] == ["canplay", "playing"]


def test_nothing_is_stopped_or_started_until_a_tv_is_chosen() -> None:
    out = _run("closed")
    assert out["cancelled"] == {"code": "cancelled", "says": False}
    assert out["failed"] == {"code": "failed", "says": True}
    assert out["before"] == 0


def test_a_stream_that_is_refused_leaves_the_tv_as_it_was_found() -> None:
    out = _run("closed")
    assert out["full"]["code"] == "too_many_streams"
    assert (out["ended"], out["loads"], out["kept"]) == ([True], 0, False)


@pytest.mark.parametrize("answers", ["false", "never"])
def test_a_tv_that_does_not_take_the_stream_is_let_go(answers: str) -> None:
    out = _run("refuses")[answers]
    assert out == {
        "tried": {"code": "failed", "says": True},
        "ended": [True],
        "kept": False,
        "timers": 0,
    }


def test_play_pause_and_seek_show_at_once_and_reach_the_tv() -> None:
    out = _run("controls")
    assert out["pausedAtOnce"] == {"paused": True, "events": ["pause"]}
    assert out["pausedThen"] == {"paused": True, "events": []}
    assert (out["playedAtOnce"], out["playedThen"]) == ({"paused": False}, ["play", "playing"])
    assert out["seeking"] == {"seeking": True, "at": 500, "events": ["seeking", "timeupdate"]}
    assert out["seeked"] == {"seeking": False, "at": 500, "events": ["seeked", "timeupdate"]}
    assert out["past"] == 3000
    assert (out["volume"], out["muted"]) == (0.6, True)
    # A dragged volume is sent once.
    assert out["calls"] == ["pause", "play", "seek 500", "seek 3000", "volume 0.6", "mute true"]


def test_what_the_tv_refuses_or_never_answers_goes_back_to_what_it_does() -> None:
    out = _run("stubborn")
    assert out["pause"] == {"paused": False, "events": ["pause", "play"]}
    assert out["seek"] == {"seeking": False, "at": 100}
    assert out["hung"] == {"seeking": False, "paused": False, "far": True}


def test_the_tvs_own_remote_is_followed() -> None:
    out = _run("remote")
    assert out["paused"] == [True, ["pause"]]
    assert out["on"] == [False, ["play", "playing"]]
    assert out["waits"] == [1, ["waiting"]]
    assert out["plays"] == [4, ["canplay", "playing"]]
    assert out["twice"] == []
    assert out["volume"] == [0.9, ["volumechange"], True]
    assert out["fixed"] is False


def test_the_place_is_told_while_it_plays_and_saving_is_left_to_the_player() -> None:
    out = _run("ticks")
    assert out["playing"] == {"updates": 6, "at": 103}
    assert (out["paused"], out["posts"]) == ([], [])


def test_another_stream_goes_to_the_same_tv_and_holds_the_place_meanwhile() -> None:
    out = _run("change")
    assert (out["during"]["at"], out["during"]["paused"], out["taken"]["at"]) == (118, True, 118)
    assert (out["gone"], out["ended"], out["sessions"]) == ([], [], ["picker"])
    assert [(x["at"], x["autoplay"], x["line"]) for x in out["loads"]] == [
        (118, False, "S1 · E4"),
        (0, True, "S1 · E5"),
    ]
    assert out["key"] == "102"


def test_of_two_streams_asked_for_the_later_plays() -> None:
    out = _run("hurried")
    assert out["first"]["code"] == "superseded"
    assert (out["stream"], out["gone"]) == ("st3", [])


def test_the_end_on_the_tv_is_the_videos_end() -> None:
    assert _run("ends") == {
        "events": ["timeupdate", "pause", "ended"],
        "at": 3000,
        "ended": True,
        "paused": True,
        "gone": [],
    }


def test_a_stream_that_fails_on_the_tv_is_an_error_at_its_place_and_another_may_follow() -> None:
    out = _run("errs")
    assert (out["events"], out["error"], out["at"]) == (["error"], {"code": 2}, 105)
    assert (out["gone"], out["after"]) == ([], None)


@pytest.mark.parametrize(
    ("how", "reason"),
    [
        ("stopped", "stopped"),
        ("another", "taken"),
        ("told", "taken"),
        ("session", "stopped"),
        ("silent", "stopped"),
    ],
)
def test_the_player_is_told_once_how_the_tv_ended_it_and_nothing_runs_on(
    how: str, reason: str
) -> None:
    out = _run("over")[how]
    assert out["gone"] == [reason]
    assert out["at"] in (107, 108)
    assert (out["kept"], out["posts"], out["late"], out["timers"]) == (False, [], 0, 0)


def test_stop_ends_the_session_on_the_tv_once_and_says_nothing_more() -> None:
    assert _run("stop") == {
        "ended": [True],
        "gone": [],
        "events": [],
        "at": 104,
        "kept": False,
        "posts": [],
        "timers": 0,
    }


def test_a_tv_that_is_lost_keeps_its_place_and_carries_on_when_it_is_back() -> None:
    out = _run("lost")
    # Told at once, hidden page or not.
    assert (out["lost"], out["after"]) == ([True, True], False)
    assert out["gone"] == {"events": ["waiting"], "at": 105, "gone": []}
    assert out["still"] == {"at": 105, "asked": ["session sess-1"], "calls": []}
    assert out["back"] == {"events": ["canplay", "playing"], "at": 135, "gone": []}


def test_a_tv_lost_for_two_minutes_is_over() -> None:
    assert _run("lostForGood") == {"before": [], "after": ["lost"], "kept": False}


def test_stopping_while_the_tv_is_lost_asks_nothing_of_a_session_that_is_gone() -> None:
    assert _run("stopLost") == {"ended": [], "kept": False, "timers": 0}


def test_a_tv_that_is_back_with_something_else_was_taken() -> None:
    assert _run("backToAnother") == ["taken"]


def test_a_reloaded_page_finds_what_plays_and_follows_it() -> None:
    out = _run("reload")
    assert (out["before"], out["scripts"]) == (0, 1)
    assert out["stream"] == {
        "stream_id": "st1",
        "position_s": 110,
        "duration_s": 3000,
        "paused": False,
        "rating_key": "101",
        "title": {"rating_key": "101", "title": "Glass Orchard", "subtitle": "S1 · E4"},
        "keep": {"quality": "low", "played_s": 50},
    }
    assert (out["name"], out["at"], out["calls"]) == ("Living room", 111, ["pause"])
    assert (out["posts"], out["beacons"]) == ([], [])


def test_where_nothing_can_be_on_the_player_is_told_at_once_and_nothing_is_loaded() -> None:
    out = _run("nothing")
    assert out["never"] == {"found": None, "scripts": []}
    assert out["old"] == {"found": None, "scripts": [], "holds": False}


@pytest.mark.parametrize("since", ["emptied", "another"])
def test_what_the_tv_says_is_over_is_forgotten(since: str) -> None:
    assert _run("nothing")[since] == {
        "found": None,
        "kept": False,
        # A session that plays nothing is ended, one that plays something else is left to it.
        "ended": [True] if since == "emptied" else [],
        # The stream is stopped at the last place known.
        "posts": [["stream/st1/stop", 100, 0]],
    }


def test_a_tv_that_plays_nothing_is_over_and_its_stream_stopped_at_the_last_place() -> None:
    assert _run("empty") == {
        "found": None,
        "kept": False,
        "posts": [["stream/st1/stop", 112, 62]],
        "ended": [True],
    }


def test_a_title_the_person_plays_here_after_all_is_not_looked_for_again() -> None:
    assert _run("released") == {
        "none": None,
        "place": {"position_s": 112, "played_s": 62, "age_s": 11},
        "twice": None,
        "other": True,
        "kept": [],
        "found": None,
        "holds": False,
        "ended": [],
    }


def test_a_tv_found_after_the_person_plays_here_is_ended_there() -> None:
    assert _run("releasedMeanwhile") == {"found": None, "ended": [True], "kept": [], "posts": []}


def test_a_tv_found_after_somebody_signs_in_is_left_to_itself() -> None:
    assert _run("clearedMeanwhile") == {"found": None, "ended": [], "posts": []}


@pytest.mark.parametrize(
    ("page", "gone", "posts"),
    [("player", ["stopped"], []), ("keeper", [], [["stream/st1/stop", 101, 1]])],
)
def test_playing_here_in_a_second_tab_ends_what_the_first_follows(
    page: str, gone: list[str], posts: list[Any]
) -> None:
    assert _run("releasedElsewhere")[page] == {
        "ended": [True],
        "gone": gone,
        "posts": posts,
        "kept": [],
    }


def test_a_title_another_tab_plays_on_a_tv_is_told_apart_from_one_that_is_over() -> None:
    assert _run("elsewhere") == {
        "same": {"code": "elsewhere", "says": False},
        "other": None,
        "scripts": [],
    }


def test_a_start_called_back_does_not_end_the_session_of_the_one_chosen_after_it() -> None:
    out = _run("chosenAgain")
    for name in ("secondPlays", "firstFailsEarly"):
        assert out[name] == {
            "pickers": 1,
            "one": {"code": "superseded", "says": False},
            "two": "taken",
            "ended": [],
            "loads": ["ticket2"],
        }
    assert out["bothFail"]["ended"] == [True]


def test_a_start_that_comes_after_a_later_one_takes_nothing_from_it() -> None:
    out = _run("chosenAgain")["firstLate"]
    assert (out["one"]["code"], out["two"], out["ended"]) == ("superseded", "taken", [])
    assert out["loads"] == ["ticket2"]


def test_a_second_press_called_back_leaves_the_first_start_to_play() -> None:
    out = _run("chosenAgain")
    assert (out["back"]["code"], out["kept"]) == ("cancelled", 1)


def test_stopping_a_start_called_back_leaves_the_session_to_the_one_that_waits_on_it() -> None:
    out = _run("chosenAgain")
    stopped = {"ended": [], "calls": ["stop"]}
    assert out["stale plays"] == {"stopped": stopped, "two": "taken", "ended": [], "loads": 2}
    # Where that one plays nothing either, the session is ended with it.
    assert (out["stale fails"]["stopped"], out["stale fails"]["ended"]) == (stopped, [True])
    assert out["stale alone"]["ended"] == [True]
    assert out["found"] == []


def test_a_tv_someone_else_took_is_not_ended_by_a_start_that_fails() -> None:
    out = _run("chosenAgain")["taken"]
    assert (out["late"]["code"], out["ended"]) == ("replaced", [])


def test_what_the_player_keeps_is_written_when_it_changes_and_only_the_place_again_and_again() -> (
    None
):
    out = _run("writes")
    assert out["beats"]["writes"] == 6
    assert out["beats"]["largest"] < 300
    assert out["place"] == 130
    assert "mediaman.watch.cast.kept" in out["changed"]
    assert out["kept"] == {"quality": "best", "markers": "y", "played_s": 30}
    assert out["others"] == {"played_s": 30}
    assert out["after"] == []


def test_a_tv_not_found_in_time_is_told_apart_from_one_that_is_over_and_not_forgotten() -> None:
    out = _run("slow")
    assert out["first"] == {"answer": {"code": "lost", "says": False}, "kept": True, "ended": []}
    assert out["second"] == {"answer": {"code": "lost", "says": False}, "kept": True}
    assert out["found"] is True


def test_a_tv_found_late_has_its_place_kept_from_then_on() -> None:
    assert _run("slow")["late"] == {"before": 0, "saves": 1}


def test_a_tv_that_says_what_it_plays_a_moment_later_is_waited_for() -> None:
    assert _run("mediaLater") is True


def test_what_another_page_has_put_on_a_tv_meanwhile_is_not_forgotten() -> None:
    assert _run("newer") == {"found": None, "stream": "st7"}


def test_the_player_of_another_title_is_told_at_once_and_the_place_is_kept_for_it() -> None:
    out = _run("otherTitle")
    assert out["found"] is None
    assert [save[1] for save in out["saves"]] == [100.5]


def test_played_time_is_each_titles_own() -> None:
    out = _run("played")
    assert out["first"][:2] == ["101", 910]
    # A quality change plays the same title on.
    assert out["quality"][:2] == ["101", 919]
    assert out["started"] == ["102", 0, None]
    assert out["next"][:2] == ["102", 10]
    assert out["set"][1] == 4
    assert out["unsaid"][1:] == [4, {"quality": "best"}]
    assert out["saved"] == ["progress", 15.5, 9]


def test_a_tv_another_tab_follows_is_left_to_it() -> None:
    out = _run("twoTabs")
    assert (out["found"], out["scripts"]) == (None, [[], []])
    assert (out["holds"], out["other"]) == (True, False)


def test_a_page_that_only_keeps_the_place_makes_way_for_a_player() -> None:
    out = _run("twoTabs")
    assert (out["followed"], out["saves"], out["after"]) == (True, 1, 1)
    assert (out["posts"], out["record"]) == ([], True)


def test_without_a_player_the_place_is_saved_every_ten_seconds_on_pause_and_at_the_end() -> None:
    out = _run("keeper")
    assert out["played"] == [
        ["progress", 100.5, 60],
        ["progress", 110.5, 70],
        ["progress", 120.5, 80],
        ["progress", 130.5, 90],
    ]
    assert out["paused"] == [["progress", 131, 91]]
    assert out["beacon"] == [
        ["/api/watch/progress", {"stream_id": "st1", "position_s": 136, "played_s": 96}]
    ]
    assert out["last"] == ["stream/st1/stop", 3000, 96]
    assert (out["record"], out["ended"], out["nowhere"], out["timers"]) == (False, [True], None, 0)


def test_a_paused_stream_is_kept_alive_without_moving_its_place() -> None:
    assert _run("keeper")["kept"] == [["progress", 131, 91]] * 2


def test_what_plays_is_shown_only_once_the_server_says_it_is_this_persons() -> None:
    out = _run("keeper")
    assert out["early"] is None
    assert (out["now"]["title"], out["now"]["position_s"]) == ("Glass Orchard", 131)


def test_the_bar_of_another_page_pauses_seeks_and_stops_with_the_place_saved() -> None:
    out = _run("keeperControls")
    assert (out["paused"], out["calls"]) == (True, ["pause", "seek 400"])
    assert out["stop"] == ["/api/watch/stream/st1/stop", {"position_s": 400, "played_s": 1}]
    assert (out["ended"], out["told"], out["now"]) == ([True], True, None)


def test_a_stream_the_server_has_ended_is_ended_on_the_tv_and_an_outage_is_not() -> None:
    out = _run("keeperGone")
    assert out["410"] == {"ended": [True], "record": False, "now": None}
    assert out["502"] == {"ended": [], "record": True, "now": None}


def test_a_play_the_player_leaves_has_its_place_kept() -> None:
    out = _run("left")
    assert (out["key"], out["before"], out["ended"]) == ("101", 0, [])
    assert [save[1] for save in out["saves"]] == [100.5, 110.5]


def test_another_title_takes_the_tv_and_the_one_before_keeps_its_place() -> None:
    out = _run("replaces")
    assert (out["pickers"], out["ended"], out["title"], out["key"]) == ([], [], "Locks", "7")
    assert out["stop"] == ["/api/watch/stream/st1/stop", {"position_s": 101, "played_s": 1}]


def test_what_ended_while_no_page_was_open_is_saved_as_played_to_the_end() -> None:
    assert _run("finishedAway") == {"posts": [["stream/st1/stop", 3000, 0]], "record": False}


def test_a_play_that_is_left_shows_at_once_with_all_a_remote_needs() -> None:
    out = _run("bar")
    assert out["before"] is None
    assert out["now"] == {
        "id": "st1",
        "name": "Living room",
        "rating_key": "101",
        "title": "Glass Orchard",
        "subtitle": "S1 · E4",
        "season": 1,
        "episode": 4,
        "episode_title": "",
        "position_s": 100,
        "duration_s": 3000,
        "paused": False,
        "lost": False,
        "state": "playing",
        "volume": 0.4,
        "muted": False,
        "can_volume": True,
        "markers": [{"kind": "intro"}],
        "chapters": [{"title": "One"}],
        "quality": "low",
        "subtitle_id": "44",
        "subtitle_tracks": [{"id": "44", "label": "English"}],
        "next": {"rating_key": "102", "label": "S1 · E5 — Windfall"},
        "upnext_in_s": None,
        "still_in_s": None,
    }
    assert out["films"] == {
        "2021": [None, None, None],
        "E7": [None, 7, ""],
        "S2 · E3 — A — b": [2, 3, "A — b"],
    }


def test_a_press_does_what_it_says_and_nothing_where_that_holds_already() -> None:
    out = _run("bar")
    assert (out["calls"], out["state"]) == (["pause"], "paused")
    assert sorted(out["after"]) == ["mute true", "play", "volume 0.7"]
    assert out["said"] == [0.7, True]


def test_a_page_without_a_player_hears_of_each_change_and_how_it_ended() -> None:
    out = _run("bar")
    assert out["buffering"] == ["buffering", ["buffering"]]
    assert out["lost"] == [True, True, True]
    assert (out["gone"], out["over"], out["at"]) == ("lost", "lost", 100.3)
    assert out["followed"] == []


def test_stopping_says_whether_the_place_is_saved_and_waits_for_no_server_for_ever() -> None:
    out = _run("stops")
    assert out["kept"]["said"] == {"position_s": 103, "saved": True}
    assert out["refused"]["said"] == {"position_s": 103, "saved": False}
    assert out["never"]["at"] == [None, None]
    assert out["never"]["said"] == {"position_s": 103, "saved": False}
    # The person's own stop is no news to them.
    assert [x["ended"] for x in out.values()] == [[True]] * 3
    assert [x["over"] for x in out.values()] == [None] * 3


@pytest.mark.parametrize(
    ("name", "asked", "load"),
    [
        ("quality", ["101", "low", "off"], ["ticket2", 110, True, "S1 · E4"]),
        ("paused", ["101", "best", "9"], ["ticket2", 100, False, "S1 · E4"]),
        ("next", ["102", "best", "off"], ["ticket2", 30, True, "S1 · E5 — Windfall"]),
    ],
)
def test_another_stream_is_put_on_the_tv_with_no_player(
    name: str, asked: list[str], load: list[Any]
) -> None:
    out = _run("other")[name]
    assert out["during"] == ["loading", "st1", "Glass Orchard"]
    assert (out["answer"], out["loads"], out["ended"]) == ("taken", [load], [])
    (body,) = out["opened"]
    assert [body["rating_key"], body["quality"], body["subtitle"]] == asked
    assert (out["now"][0], out["now"][2:5]) == ("st2", asked)
    assert (out["now"][5], out["now"][7], out["kept"]) == ([2], 1, "st2")
    assert out["busy"] is False


def test_the_next_episode_ends_the_one_before_at_its_place_and_knows_what_follows() -> None:
    out = _run("other")["next"]
    assert out["posts"][1] == ["stream/st1/stop", 110, 10]
    # A subtitle track is one film's own.
    assert _run("other")["track"] == ["102", False]
    assert out["now"][6] == {"rating_key": "103", "label": "S1 · E6"}
    # Its own played time, at its own place.
    assert out["saves"] == [["st2", 30.5, 0], ["st2", 40.5, 10]]


@pytest.mark.parametrize(
    ("name", "code"), [("noPage", "unreachable"), ("refused", "rate_limited"), ("lost", "failed")]
)
def test_a_stream_that_is_refused_leaves_what_plays(name: str, code: str) -> None:
    out = _run("other")[name]
    assert out["answer"]["code"] == code
    assert (out["loads"], out["ended"], out["now"][:2]) == ([], [], ["st1", "playing"])
    assert out["now"][3] == "best"


@pytest.mark.parametrize("name", ["gone", "fell"])
def test_a_refused_start_that_ended_the_stream_before_opens_that_again(name: str) -> None:
    out = _run("other")[name]
    assert out["answer"]["code"] == "unreachable"
    assert out["loads"] == [["ticket2", 110, True, "S1 · E4"]]
    assert out["now"][:4] == ["st2", "playing", "101", "best"]
    assert out["ended"] == []


def test_a_tv_that_does_not_take_another_stream_is_let_go_with_the_place_saved() -> None:
    out = _run("other")["noTv"]
    assert (out["answer"]["code"], out["now"], out["ended"]) == ("failed", None, [True])
    assert out["posts"][-1] == ["stream/st2/stop", 110, 10]


def test_of_two_changes_the_later_plays_and_the_stream_of_the_first_is_stopped() -> None:
    assert _run("otherTwice") == {
        "one": {"code": "superseded", "says": False},
        "two": "taken",
        "loads": ["medium"],
        "stopped": ["/api/watch/stream/q-low/stop"],
        "id": "q-medium",
    }


def test_the_old_streams_end_is_no_end_while_another_is_on_its_way() -> None:
    out = _run("savesMeanwhile")
    assert out["told"] == ["loading"]
    # Nor is its place saved, or an answer to a save taken for the end.
    assert out["quiet"] == ["/api/cast/stream"]
    assert (out["posts"], out["state"], out["ended"]) == (["/api/cast/stream"], "loading", [])


def test_the_next_episode_begins_by_itself_with_no_player_open() -> None:
    out = _run("upnext")["counted"]
    assert out["counted"] == ["upnext", 10, None, "101"]
    assert out["stopped"] == [["stream/st1/stop", 3000, 1]]
    assert (out["later"], out["record"]) == (["upnext", 4, None, "101"], "st1")
    assert (out["then"], out["ended"]) == (["playing", None, None, "102"], [])


def test_a_cancelled_count_waits_for_a_press() -> None:
    out = _run("upnext")["cancelled"]
    assert out["cancelled"] == out["still"] == ["upnext", None, None, "101"]
    assert (out["loads"], out["pressed"]) == (1, 2)


def test_without_autoplay_nothing_counts_and_a_stop_ends_it_with_its_end_saved() -> None:
    out = _run("upnext")
    assert out["off"] == ["upnext", None, None, "101"]
    assert out["stop"] == {
        "said": {"position_s": 3000, "saved": True},
        "posts": [["stream/st1/stop", 3000, 1]],
        "ended": [True],
    }


def test_where_nothing_follows_the_end_is_the_end() -> None:
    assert _run("upnext")["film"] == {
        "now": None,
        "over": "ended",
        "posts": [["stream/st1/stop", 3000, 1]],
        "ended": [True],
    }


def test_after_three_episodes_by_themselves_somebody_is_asked_and_silence_ends_it() -> None:
    out = _run("upnext")["asked"]
    assert out["shown"] == ["upnext", "upnext", "upnext", "still"]
    assert (out["asked"], out["in"], out["loads"]) == (["still", None], 589, 4)
    # Each end was saved once.
    assert out["stops"] == 4
    assert (out["over"], out["ended"], out["record"]) == ("stopped", [True], False)


def test_a_press_meanwhile_is_a_sign_of_somebody_and_two_hours_without_one_are_not() -> None:
    out = _run("upnext")
    assert (out["signs"], out["long"]) == ("upnext", "still")
    # Nothing begins by itself without autoplay, so nobody is asked.
    assert out["longOff"] == "upnext"


def test_a_next_episode_that_cannot_begin_waits_for_a_press() -> None:
    assert _run("upnext")["failed"] == {"now": ["upnext", None, None, "101"], "ended": []}


def test_a_stop_while_the_old_stream_is_opened_again_stops_that_too_and_ends_it_once() -> None:
    out = _run("reopening")["stop"]
    assert out["opening"] is True
    assert out["stopped"] == ["/api/watch/stream/st1/stop", "/api/watch/stream/re2/stop"]
    assert (out["ended"], out["heard"], out["now"]) == ([True], [], None)


def test_a_stop_before_the_old_stream_is_opened_again_opens_none() -> None:
    out = _run("reopening")["stopEarly"]
    assert (out["asked"], out["ended"], out["now"]) == (0, [True], None)


@pytest.mark.parametrize("when", ["change", "changeEarly"])
def test_another_choice_while_the_old_stream_is_opened_again_plays(when: str) -> None:
    out = _run("reopening")[when]
    assert (out["opening"], out["first"], out["second"]) == (True, "unreachable", "taken")
    assert out["stopped"] == ["/api/watch/stream/re2/stop"]
    assert (out["ended"], out["heard"], out["now"]) == ([], [], ["medium", "playing"])


def test_the_count_goes_on_where_the_person_goes_and_the_next_episode_begins_there() -> None:
    out = _run("waits")["load"]
    assert out["taken"] == ["upnext", 6, None, "101", "Glass Orchard"]
    assert out["then"] == ["playing", None, None, "102", "Glass Orchard"]
    # The end was saved by the page before.
    assert (out["ended"], out["posts"], out["auto"]) == ([], ["/api/cast/stream"], 1)


def test_the_count_goes_on_where_the_receiver_has_forgotten_what_it_played() -> None:
    assert _run("waits")["cleared"] == ["upnext", 10, None, "101", "Glass Orchard"]


def test_a_slow_start_of_the_next_episode_is_one_start() -> None:
    assert _run("waits")["slow"] == {"asked": 1, "auto": 1}


def test_a_count_cancelled_while_another_page_looks_is_taken_up_cancelled() -> None:
    out = _run("waits")["heard"]
    assert out["stored"] == {"kind": "upnext", "until": None}
    assert out["now"] == ["upnext", None]


def test_a_title_that_waits_at_its_end_is_not_held() -> None:
    assert _run("waits")["holds"] is False


def test_a_second_tab_takes_the_count_and_gives_it_back_and_one_episode_begins() -> None:
    out = _run("waits")
    assert out["tab"]["first"] is None
    assert (out["tab"]["second"][0], out["tab"]["ended"]) == ("upnext", [])
    assert (out["back"]["first"][0], out["back"]["second"]) == ("upnext", None)
    assert (out["back"]["ended"], out["back"]["loads"]) == ([], 2)


@pytest.mark.parametrize(
    ("kind", "now"),
    [
        ("cancelled", ["upnext", None, None]),
        ("still", ["still", None, 599]),
        # One that ran out with no page open begins nothing by itself.
        ("old", ["upnext", None, None]),
    ],
)
def test_what_waits_for_a_press_is_taken_up_as_it_was(kind: str, now: list[Any]) -> None:
    out = _run("waits")[kind]
    assert (out["now"][:3], out["ended"], out["loads"]) == (now, [], 1)


def test_a_question_nobody_answered_is_over_on_the_next_page_too() -> None:
    out = _run("waits")["asked"]
    assert (out["now"], out["ended"]) == (None, [True])


def test_the_sitting_is_counted_on_across_pages() -> None:
    assert _run("waits")["sitting"] == "still"


def test_what_ended_while_no_page_was_open_offers_the_next_episode_uncounted() -> None:
    out = _run("waits")["away"]
    assert out["now"][:3] == ["upnext", None, None]
    assert (out["posts"], out["ended"]) == ([["stream/st1/stop", 3000, 1]], [])


def test_a_count_on_a_tv_that_plays_something_else_is_over_and_the_tv_left_alone() -> None:
    assert _run("waits")["taken"] == {"now": None, "ended": [], "record": False}


@pytest.mark.parametrize(
    ("key", "state"),
    [("101", ["upnext", None]), ("102", ["upnext", None]), ("999", ["upnext", 10])],
)
def test_a_player_of_the_ended_or_the_next_title_ends_the_count_everywhere(
    key: str, state: list[Any]
) -> None:
    out = _run("waits")[f"player {key}"]
    assert (out["answer"], out["ended"]) == (None, [])
    assert out["first"] == out["here"] == state


@pytest.mark.parametrize("kind", ["changing", "idle"])
def test_a_page_that_only_looks_ends_nothing_of_a_page_that_is_open(kind: str) -> None:
    assert _run("looksOnly")[kind] == {"now": False, "ended": [], "posts": [], "record": True}


def test_what_is_over_on_the_tv_of_a_page_that_is_gone_is_ended() -> None:
    out = _run("looksOnly")["dead"]
    assert (out["ended"], out["posts"], out["record"]) == ([True], ["stream/st1/stop"], False)


def test_a_play_left_as_it_changed_its_stream_is_opened_again_once() -> None:
    out = _run("leftChanging")
    assert out["asked"] == [["101", "best"]]
    assert out["now"] == ["st9", "playing", "best", "102"]
    assert (out["going"], out["loads"]) == (True, 2)
    assert (out["after"], out["ended"]) == (None, [True])


def test_a_script_that_throws_leaves_stop_and_pause_working() -> None:
    out = _run("sdkFails")
    assert (out["now"], out["calls"]) == ([103, True], ["pause"])
    assert out["said"] == {"position_s": 103, "saved": True}
    assert (out["ended"], out["after"], out["record"]) == ([True], None, False)


def test_a_title_is_held_only_while_this_page_or_a_live_one_plays_it() -> None:
    assert _run("holds") == {
        "follows": [True, False],
        "elsewhere": [True, False],
        "left": False,
        "notFound": False,
        "early": False,
        "keeps": True,
        "seen": True,
        "crashed": False,
    }
