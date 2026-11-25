"""player.js in a fake DOM: quality switches, the next-episode card, keys and controls.

Each test renders the real player markup, runs player.js on it through
``player_harness.js`` under node, and checks what the person would see.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import jinja2
import pytest

from mediaman.services.media_meta.plex_quality import quality_rows
from mediaman.services.media_meta.plex_watch import PlexItem

REPO = Path(__file__).resolve().parents[4]
HARNESS = Path(__file__).with_name("player_harness.js")
# The page's own markup, without the site chrome around it.
_ENV = jinja2.Environment(
    loader=jinja2.ChoiceLoader(
        [
            jinja2.DictLoader(
                {
                    "base.html": "<title>{% block title %}{% endblock %}</title>"
                    "{% block content %}{% endblock %}"
                }
            ),
            jinja2.FileSystemLoader(REPO / "src/mediaman/web/templates"),
        ]
    ),
    autoescape=True,
)

EPISODE = {
    "item": {
        "rating_key": "501",
        "title": "The Long Coast",
        "subtitle": "S2 · E4",
        "duration_s": 3000.0,
    },
    "start_s": 0,
    "back_href": "/watch/show/77",
    "next": {"href": "/watch/play/502", "label": "S2 · E5 — High Tide"},
    "show_href": "/watch/show/77",
    "leaving": None,
    "qualities": quality_rows(
        PlexItem("episode", "501", "Low Water", "", None, 3000, "2", None, False)
    ),
}


def _markup(tmp_path_factory, context: dict[str, Any]) -> Path:
    html = _ENV.get_template("watch_player.html").render(**context)
    path = tmp_path_factory.mktemp("player") / "player.html"
    path.write_text(html)
    return path


@pytest.fixture(scope="module")
def markup(tmp_path_factory) -> Path:
    return _markup(tmp_path_factory, EPISODE)


@pytest.fixture(scope="module")
def last_episode(tmp_path_factory) -> Path:
    return _markup(tmp_path_factory, {**EPISODE, "next": None})


def run(
    markup: Path,
    scenario: str,
    *,
    reduced: bool = False,
    saved: dict[str, str] | None = None,
    plays: str | None = None,
    pages: dict[str, Path] | None = None,
) -> Any:
    args = ["node", str(HARNESS), str(markup), scenario] + (["reduced"] if reduced else [])
    args += [json.dumps(saved)] if saved or plays else []
    args += [plays] if plays else []
    others = json.dumps({href: str(path) for href, path in (pages or {}).items()})
    env = {**os.environ, "PLAYER_PAGES": others}
    done = subprocess.run(args, capture_output=True, text=True, timeout=30, env=env)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_quality_switch_holds_the_last_frame_until_the_new_stream_plays(markup):
    out = run(markup, "quality_switch")
    assert out["whileLoading"]["hold"] == {"shown": True, "drewVideo": True}
    assert out["afterManifest"]["hold"]["shown"] is True
    assert out["afterPlaying"]["hold"]["shown"] is False


@pytest.mark.parametrize("way", ["told", "untold", "never"])
def test_the_last_frame_stays_until_the_new_one_is_on_screen_and_then_fades(markup, way):
    out = run(markup, "hold_released", plays=way)
    assert (out["seeking"], out["seeked"]) == (True, True)
    assert (out["seen"], out["fading"], out["after"]) == (False, True, True)


def test_the_last_frame_stays_when_another_quality_is_picked_before_it_is_seen(markup):
    assert run(markup, "hold_released", plays="told")["again"] is True


def test_a_quality_switch_shows_loading_rather_than_pause(markup):
    out = run(markup, "quality_switch")
    loading = out["whileLoading"]
    assert loading["loading"] is True
    assert loading["play"]["busy"] == "true"
    assert loading["play"]["icon"] is None
    assert loading["play"]["ring"] is True
    assert out["afterPlaying"]["play"] == {
        "label": "Pause",
        "busy": None,
        "icon": "#i-pause",
        "ring": False,
    }


def test_a_held_frame_is_marked_until_the_new_stream_plays(markup):
    # The stylesheet keeps the centre spinner back for a moment under .is-held.
    out = run(markup, "quality_switch")
    assert out["whileLoading"]["loading"] is True
    assert out["whileLoading"]["held"] is True
    assert out["afterManifest"]["held"] is True
    assert out["afterPlaying"]["held"] is False


def test_a_plain_stall_still_shows_the_centre_spinner(markup):
    out = run(markup, "stall")
    assert out == {"loading": True, "held": False}


def test_a_refused_quality_goes_back_to_the_one_that_played_and_keeps_the_frame(markup):
    out = run(markup, "quality_refused")
    assert out["beforePlaying"]["qualities"] == ["auto", "low", "low", "auto"]
    assert out["beforePlaying"]["hold"]["shown"] is True
    assert out["afterPlaying"]["hold"]["shown"] is False
    assert out["checked"] == "best"
    assert out["saved"] == "auto"
    assert [t["kind"] for t in out["toasts"]] == ["error"]
    assert "Best" in out["toasts"][0]["text"]


@pytest.mark.parametrize(
    ("stored", "plays", "ticked", "pill"),
    [
        (None, "1080p-8000", "auto", "Auto · 1080p"),
        ("auto", "720p-4000", "auto", "Auto · 720p"),
        ("original", "original", "original", "Original · 1080p"),
        ("1080p-10000", "1080p-10000", "1080p-10000", "1080p · 10 Mbps"),
        # Not offered for this title: the nearest lower one.
        ("1080p-20000", "1080p-10000", "1080p-10000", "1080p · 10 Mbps"),
        ("720p-3000", "480p-1500", "480p-1500", "480p · 1.5 Mbps"),
        # Choices of an earlier version of the page.
        ("best", "best", "1080p-8000", "1080p · 8 Mbps"),
        ("low", "low", "480p-1500", "480p · 1.5 Mbps"),
    ],
)
def test_the_menu_lists_what_the_server_offers_and_ticks_what_plays(
    markup, stored, plays, ticked, pill
):
    saved = {"mediaman.watch.quality": stored} if stored else None
    out = run(markup, "quality_menu", saved=saved, plays=plays)
    offered = ["auto", "original", "1080p-10000", "1080p-8000", "720p-4000", "480p-1500"]
    assert out["ids"] == offered
    assert (out["ticked"], out["pill"]) == ([ticked], pill)
    assert out["starts"] == [stored or "auto"]
    # What plays in its place never replaces the choice.
    assert out["saved"] == stored


def test_automatic_says_what_it_plays(markup):
    out = run(markup, "quality_menu", plays="720p-4000")
    assert out["auto"] == "720p · 4 Mbps"
    out = run(markup, "quality_menu", saved={"mediaman.watch.quality": "best"}, plays="best")
    assert out["auto"] == ""


def test_a_saved_choice_the_server_does_not_know_plays_automatic_and_stays_saved(markup):
    out = run(markup, "quality_unknown", saved={"mediaman.watch.quality": "8k-dolby"})
    assert out["starts"] == ["8k-dolby", "auto"]
    assert (out["ticked"], out["saved"], out["message"]) == (["auto"], "8k-dolby", False)


def test_choosing_what_automatic_already_plays_starts_nothing(markup):
    out = run(markup, "quality_same")
    assert out["starts"] == ["auto"]
    assert (out["ticked"], out["saved"]) == (["1080p-8000"], "1080p-8000")


def test_an_original_the_browser_cannot_play_becomes_the_best_conversion(markup):
    out = run(markup, "original_unplayable")
    assert out["starts"] == ["auto", "original", "1080p-10000"]
    assert (out["ticked"], out["saved"]) == (["1080p-10000"], "original")
    assert "1080p · 10 Mbps" in out["toasts"][0]["text"]


def test_automatic_steps_down_when_playback_keeps_stalling_and_not_for_seeks(markup):
    out = run(markup, "auto_steps")
    assert out["afterSeeks"] == out["afterOne"] == out["afterFarApart"] == ["auto"]
    assert out["afterTwo"]["starts"] == ["auto", "720p-4000"]
    assert out["afterTwo"]["ticked"] == ["auto"]
    assert out["afterTwo"]["pill"] == "Auto · 720p"
    assert out["afterTwo"]["saved"] is None
    assert out["shortWait"] == ["auto", "720p-4000"]
    assert out["longWait"] == out["atTheBottom"] == ["auto", "720p-4000", "480p-1500"]


def test_automatic_steps_up_late_and_later_still_when_that_stalled(markup):
    out = run(markup, "auto_steps")
    assert out["notYet"] == out["slowLine"] == out["longWait"]
    assert out["up"] == [*out["longWait"], "720p-4000"]
    assert out["downAgain"] == [*out["up"], "480p-1500"]
    assert out["damped"] == out["downAgain"]
    assert out["upAgain"] == [*out["downAgain"], "720p-4000"]


def test_automatic_goes_straight_to_what_a_slow_connection_can_carry(markup):
    assert run(markup, "auto_jumps") == ["auto", "480p-1500"]


def test_automatic_steps_down_when_the_video_cannot_even_start(markup):
    out = run(markup, "auto_never_starts", plays="1080p-8000")
    assert out == {"once": ["auto"], "twice": ["auto", "720p-4000"]}
    saved = {"mediaman.watch.quality": "1080p-8000"}
    out = run(markup, "auto_never_starts", saved=saved, plays="1080p-8000")
    assert out["twice"] == ["1080p-8000"]


def test_automatic_gives_up_with_a_message_when_nothing_can_be_loaded(markup):
    out = run(markup, "never_loads")
    assert (out["message"], out["retry"]) == (True, True)
    assert "480p-1500" in out["starts"]
    assert len(out["starts"]) < 8


def test_automatic_gives_up_with_a_message_when_its_steps_are_refused(markup):
    out = run(markup, "never_loads", plays="refused")
    assert (out["message"], out["retry"]) == (True, True)
    assert "720p-4000" in out["starts"]
    assert len(out["starts"]) < 8


def test_automatic_gives_up_with_a_message_when_each_step_down_fails(markup):
    out = run(markup, "step_fails")
    assert out["message"] is True
    assert "720p-4000" in out["starts"]
    assert len(out["starts"]) < 12


def test_an_original_that_was_left_is_not_gone_back_to(markup):
    saved = {"mediaman.watch.quality": "original"}
    out = run(markup, "never_loads", saved=saved)
    assert (out["message"], out["retry"]) == (True, True)
    assert out["starts"] == ["original", "1080p-10000", "1080p-10000"]
    out = run(markup, "original_left", saved=saved)
    assert out["starts"] == ["original", "original", "1080p-10000", "720p-4000", "1080p-10000"]
    assert (out["ticked"], out["saved"]) == (["1080p-10000"], "original")
    assert "Staying on 1080p · 10 Mbps" in out["toasts"][-1]["text"]


def test_a_connection_that_fails_under_an_original_that_played_gets_one_more_try(markup):
    saved = {"mediaman.watch.quality": "original"}
    out = run(markup, "original_restarts", saved=saved, plays="networkError")
    assert out["once"] == ["original"] * 3
    assert out["twice"] == ["original"] * 3 + ["1080p-10000"]
    out = run(markup, "original_restarts", saved=saved, plays="mediaError")
    assert out["once"] == ["original", "original", "1080p-10000"]


def test_the_quality_that_failed_can_be_picked_to_start_it_again(markup):
    out = run(markup, "pick_after_failure")
    assert out == {"started": ["1080p-8000"], "message": False}


def test_the_ticked_quality_picked_under_an_error_tries_again(markup):
    out = run(markup, "pick_ticked")
    assert out == {"ticked": "auto", "started": ["auto"], "message": False, "final": [2, True]}


def test_automatic_picked_replaces_a_saved_choice_the_server_does_not_know(markup):
    saved = {"mediaman.watch.quality": "4320p-90000"}
    out = run(markup, "pick_over_unknown", saved=saved)
    assert out == {"before": "4320p-90000", "saved": "auto", "starts": 2}


def test_automatic_never_steps_above_what_it_began_with(markup):
    assert run(markup, "up_waits")["atItsTop"] == 1


def test_a_stall_that_is_over_in_time_steps_nowhere(markup):
    out = run(markup, "up_waits")
    assert (out["afterAShortStall"], out["down"]) == (1, 2)


def test_automatic_steps_up_neither_near_the_end_nor_under_an_open_menu(markup):
    out = run(markup, "up_waits")
    assert (out["nearTheEnd"], out["menuOpen"]) == (2, 2)


def test_a_step_up_that_is_refused_waits_longer_for_the_next_try(markup):
    out = run(markup, "up_waits")
    assert out["refused"] == ["1080p-8000", "720p-4000"]
    assert (out["waitsLonger"], out["again"]) == (0, ["1080p-8000"])


def test_a_frame_refused_late_by_the_stream_before_turns_nothing_off(markup):
    assert run(markup, "frame_trouble")["newStream"] is True


def test_a_frame_that_came_sets_the_wait_after_a_refusal_back(markup):
    assert run(markup, "frame_trouble")["waitedAgain"] == 1


def test_a_frame_that_cannot_be_shown_turns_the_previews_off(markup):
    out = run(markup, "frame_trouble")
    assert (out["shown"], out["afterError"]) == (True, False)


def test_a_page_left_while_it_loads_saves_and_stops_its_stream(markup):
    out = run(markup, "cancelled")
    assert (out["starts"], out["after"]) == (1, 1)
    assert [b["url"] for b in out["left"]] == ["/api/watch/stream/s1/stop"]
    assert out["left"][0]["body"]["position_s"] == 105


def test_a_connection_that_failed_goes_on_with_its_stream(markup):
    out = run(markup, "cancelled")
    assert (out["restarted"], out["stopped"]) == (1, [])


def test_each_quality_the_person_tries_may_be_refused_without_ending_the_video(markup):
    assert run(markup, "picks_refused") == {"message": False, "starts": 7}


def test_a_picked_quality_never_changes_by_itself(markup):
    saved = {"mediaman.watch.quality": "1080p-8000"}
    assert run(markup, "fixed_stalls", saved=saved) == ["1080p-8000"]
    # Nor when a stall that Automatic was waiting out goes on after the pick.
    assert run(markup, "picked_in_stall") == {"starts": ["auto"], "saved": "1080p-8000"}


def test_a_refused_step_goes_back_without_a_word(markup):
    out = run(markup, "auto_refused")
    assert out["starts"] == ["auto", "720p-4000", "auto"]
    assert (out["ticked"], out["saved"], out["toasts"]) == (["auto"], None, [])


def test_cancel_waits_for_the_card_to_leave_then_returns_focus(markup):
    out = run(markup, "upnext_cancel")
    assert out["shown"]["hidden"] is False
    assert out["afterClick"] == {"hidden": False, "leaving": True}
    assert out["afterExit"] == {"hidden": True, "leaving": False, "focus": True}
    assert out["href"] == "/watch/play/1"


@pytest.mark.parametrize("what", ["skip", "bubble", "menu", "message"])
def test_what_goes_plays_its_exit_and_then_hides(markup, what):
    out = run(markup, "exits")[what]
    there = {"hidden": False, "leaving": False, "inert": False}
    assert out["before"] == there
    # Out of reach at once.
    assert out["going"] == {"hidden": False, "leaving": True, "inert": True}
    assert out["still"] is True
    assert out["after"] == {"hidden": True, "leaving": False, "inert": False}


@pytest.mark.parametrize("what", ["skip", "bubble", "menu", "card", "message"])
def test_what_is_shown_again_as_it_goes_stays(markup, what):
    out = run(markup, "shown_again")[what]
    assert out == {"left": True, "hidden": False, "leaving": False, "inert": False}


@pytest.mark.parametrize("what", ["skip", "bubble", "menu", "message"])
def test_under_reduced_motion_what_goes_hides_at_once(markup, what):
    out = run(markup, "exits", reduced=True)[what]
    assert out["going"] == {"hidden": True, "leaving": False, "inert": False}


def test_a_menu_opened_again_as_it_goes_is_there_at_once(markup):
    out = run(markup, "exits")
    assert out["back"] == {"hidden": False, "leaving": False, "inert": False}
    assert out["focus"] is True


def test_the_countdown_fill_lasts_the_countdown(markup):
    assert run(markup, "upnext_cancel")["shown"]["fill"] == "10s"


def test_cancel_under_reduced_motion_hides_the_card_at_once(markup):
    out = run(markup, "upnext_cancel", reduced=True)
    assert out["afterClick"]["hidden"] is True


@pytest.mark.parametrize("way", ["click", "touch", "space", "escape", "button"])
def test_a_stop_during_the_countdown_stays_at_the_end_and_never_replays(markup, way):
    out = run(markup, "upnext_stops")
    assert out[way] == {"counting": "Pause", "replayed": 0, "leaving": True, "after": "Play"}
    assert out["href"] == "/watch/play/1"


def test_after_the_countdown_only_play_itself_replays(markup):
    out = run(markup, "upnext_stops")
    assert (out["pictureReplays"], out["buttonReplays"]) == (0, 1)


def test_space_pauses_after_the_mouse_pressed_a_control(markup):
    out = run(markup, "space")
    assert out["afterMouse"] == {"skippedOnce": True, "paused": True}
    assert out["afterMenu"] == {"paused": True, "menuClosed": True}
    assert out["afterSlider"] == {"paused": True}


def test_space_presses_the_control_tab_reached(markup):
    assert run(markup, "space")["afterTab"] == {"skipped": 10, "paused": False}


def test_controls_hide_after_a_mouse_press_and_a_key_but_stay_for_tab(markup):
    assert run(markup, "idle") == {"afterMouse": True, "afterTab": False, "afterOptionTab": False}


def test_a_seek_while_the_playlist_loads_moves_where_the_video_starts(markup):
    assert run(markup, "early_seek") == {"whilePlaylistLoads": 20, "beforeVideo": 30}


@pytest.mark.parametrize("pointer", ["mouse", "touch"])
def test_a_press_away_from_an_open_menu_only_closes_it(markup, pointer):
    out = run(markup, "click_away")
    assert out[pointer] == {"wasOpen": True, "open": False, "paused": False, "idle": False}
    assert out["next"] == {"paused": True}


def test_play_under_an_error_tries_again(markup):
    out = run(markup, "error_retry")
    assert out["shown"] is True
    assert out["afterPlay"] == {"started": 1, "shown": False, "busy": "true"}


def test_try_again_hands_its_focus_to_play(markup):
    out = run(markup, "error_retry")
    assert (out["retryFocused"], out["focusOnPlay"]) == (True, True)


def test_play_is_off_under_an_error_no_new_try_can_mend(markup):
    assert run(markup, "error_final") == {
        "text": "No longer available.",
        "retry": False,
        "playDisabled": True,
        "focusOnBack": True,
    }


def test_volume_and_mute_carry_to_the_next_video(markup):
    out = run(
        markup,
        "volume",
        saved={"mediaman.watch.volume": "0.6", "mediaman.watch.muted": "true"},
    )
    start = out["start"]
    assert (start["volume"], start["muted"], start["mute"]) == (0.6, True, "Unmute")
    assert out["unmuted"] == {"mediaman.watch.volume": "0.25", "mediaman.watch.muted": "false"}
    assert out["muted"] == {"mediaman.watch.volume": "0.25", "mediaman.watch.muted": "true"}


@pytest.mark.parametrize("stored", ["loud", "7", "-1", "", "NaN"])
def test_a_saved_volume_that_makes_no_sense_is_ignored(markup, stored):
    start = run(markup, "volume", saved={"mediaman.watch.volume": stored})["start"]
    assert (start["volume"], start["muted"]) == (1, False)


def test_the_seek_bar_answers_the_keys_of_a_slider(markup):
    assert run(markup, "seek_keys") == {
        "ArrowUp": 10,
        "ArrowDown": -10,
        "PageUp": 60,
        "PageDown": -60,
    }


def test_a_key_on_the_seek_bar_shows_for_a_moment_where_it_led(markup):
    out = run(markup, "seek_key_bubble")
    assert out["elsewhere"] is False
    assert out["ArrowRight"] == {"shown": True, "time": "10:20", "frame": 124, "stale": False}
    assert out["PageUp"] == {"shown": True, "time": "11:20", "frame": 136, "stale": False}
    # A second key starts the moment again.
    assert (out["ArrowRightStill"], out["PageUpStill"], out["gone"]) == (True, True, True)
    # The mark of the pointer is the mouse's alone.
    assert (out["marker"], out["underMouse"]) == (False, "25:00")


def test_full_screen_is_an_icon_that_follows_the_state(markup):
    out = run(markup, "fullscreen")
    enter = {"icon": "#i-expand", "label": "Full screen", "title": "Full screen (F)", "text": ""}
    leave = {
        "icon": "#i-compress",
        "label": "Exit full screen",
        "title": "Exit full screen (F)",
        "text": "",
    }
    assert out["start"] == out["left"] == enter
    assert out["entered"] == out["enteredWebkit"] == leave


def test_the_button_f_and_a_double_click_ask_for_full_screen_but_a_double_tap_does_not(markup):
    out = run(markup, "fullscreen")
    assert (out["asked"], out["askedAfterTouch"]) == (3, 3)


def test_the_volume_track_fills_to_the_volume_heard(markup):
    out = run(markup, "volume", saved={"mediaman.watch.volume": "0.6"})
    assert (out["start"]["fill"], out["unmutedFill"], out["mutedFill"]) == ("60%", "25%", "0%")


def test_the_bar_shows_frames_at_once_and_while_the_mouse_moves_along_it(markup):
    out = run(markup, "frame_hover")
    assert out["atOnce"] == [60]
    assert out["first"] == {"shown": True, "time": "5:00", "frame": 60, "stale": False}
    # 800 ms of movement: one at once, then one about every 300 ms, where the mouse is by then.
    assert len(out["moving"]) == 3
    assert out["moving"] == sorted(out["moving"])
    # What shows meanwhile is the nearest there is.
    assert (out["near"]["frame"], out["near"]["stale"]) == (out["moving"][-1], True)
    # And the place the mouse came to rest at.
    assert (out["trailing"], out["rested"]["frame"], out["rested"]["stale"]) == (84, 84, False)


def test_one_frame_is_asked_for_at_a_time_and_none_that_is_kept(markup):
    out = run(markup, "frame_hover")
    assert len(out["oneInFlight"]) == 1
    assert out["latest"] == [*out["oneInFlight"], 240]
    assert out["arrived"] == {"shown": True, "time": "20:00", "frame": 240, "stale": False}
    assert (out["nearest"]["frame"], out["nearest"]["stale"]) == (84, True)
    assert out["kept"] == {"shown": True, "time": "7:00", "frame": 84, "stale": False}
    assert out["keptAsked"] == 0


def test_looking_along_the_bar_moves_neither_the_video_nor_what_a_screen_reader_hears(markup):
    out = run(markup, "frame_hover")
    assert (out["now"], out["nowAfter"], out["position"]) == ("30", "30", 30)


def test_the_preview_stays_after_a_click_while_the_mouse_is_still_on_the_bar(markup):
    out = run(markup, "bar_left")
    assert (out["mouse"], out["left"], out["droppedOff"]) == (True, False, False)
    assert (out["touch"], out["pen"]) == (False, False)


@pytest.mark.parametrize("way", ["frame_hover", "bar_clicked"])
def test_a_mouse_that_rests_on_the_bar_hides_preview_and_controls_while_it_plays(markup, way):
    out = run(markup, way)
    rested = out["restedOnBar"] if way == "frame_hover" else out
    shown = {"idle": False, "bubble": True}
    assert (rested["paused"], rested["atFirst"]) == (shown, shown)
    assert rested["later"] == {"idle": True, "bubble": False}
    assert rested["moved"] == shown


def test_the_preview_goes_as_the_mouse_leaves_the_bar(markup):
    out = run(markup, "frame_hover")
    assert (out["left"]["shown"], out["idleAfter"]) == (False, True)


def test_a_finger_on_the_bar_shows_frames_and_lets_go_at_the_place_shown(markup):
    out = run(markup, "frame_drag")
    assert out["before"] is False
    assert (out["during"]["shown"], out["during"]["time"], out["duringAt"]) == (True, "10:00", 30)
    assert out["asked"] == [60, 120]
    assert (out["after"], out["at"]) == (False, 600)


def test_only_the_newest_frames_are_kept(markup):
    assert run(markup, "frame_many") == {"kept": 60}


@pytest.mark.parametrize(
    ("missing", "asked", "shown"),
    [
        # A frame that is not there is not asked for again.
        ("one", [60, 120], 120),
        ("between", [60, 120, 180, 240], 240),
        # A video may be a little longer than its frames.
        ("last", [599, 598, 596, 240], 240),
    ],
)
def test_a_frame_that_is_missing_leaves_the_others(markup, missing, asked, shown):
    out = run(markup, "frame_missing", plays=missing)
    assert out == {"asked": asked, "on": True, "shown": shown}


def test_two_frames_that_are_missing_turn_the_previews_off(markup):
    out = run(markup, "frame_missing", plays="two")
    assert out == {"asked": [60, 120], "on": False, "shown": None}


def test_a_stream_that_is_gone_shows_the_time_alone_until_the_next_stream(markup):
    out = run(markup, "frame_refused", plays="410")
    assert out["first"] == {"shown": True, "time": "5:00", "frame": None, "stale": True}
    assert out["atOnce"] == out["longerAfter"] == 1
    # It is no reason to start the stream again.
    assert out["starts"] == 1
    assert out["newStream"] == 1


@pytest.mark.parametrize("status", ["429", "502", "503"])
def test_frames_are_asked_for_less_and_less_while_they_are_refused(markup, status):
    out = run(markup, "frame_refused", plays=status)
    assert (out["atOnce"], out["after"]) == (1, 2)
    assert (out["longer"], out["longerAfter"]) == (2, 3)


def test_a_title_without_previews_asks_for_none_and_shows_the_time(markup):
    out = run(markup, "frame_none")
    assert (out["shown"], out["time"], out["frame"], out["asked"]) == (True, "5:00", None, [])


def test_the_bar_marks_chapters_intro_and_credits(markup):
    out = run(markup, "chapters")
    # Chapters 4 px after another, or at either end of the bar, get no gap.
    assert out["gaps"] == ["10%", "50%"]
    assert out["bands"] == [["2%", "3%"], ["80%", "1.6666666666666667%"], ["90%", "10%"]]
    # The last reaches the end of the bar, which needs no cut there.
    assert out["uncut"] == [[], ["90%"]]
    assert out["bubble"] == ["Cold open", "1:30 · Intro"]


def test_the_chapters_menu_names_the_current_chapter_and_goes_to_the_one_chosen(markup):
    out = run(markup, "chapters")
    assert out["hidden"] is False
    assert [" ".join(r.split()) for r in out["rows"]][:3] == [
        "Cold open 0:00",
        "Arrival 5:00",
        "<b>Chase</b> 5:12",
    ]
    assert out["start"] == {
        "ticked": ["0"],
        "name": "Chapters: Cold open",
        "spoken": "0:30 of 50:00, Cold open",
    }
    assert (out["focus"], out["at"], out["menuOpen"]) == ("0", 1500, "false")
    assert out["after"] == {
        "ticked": ["3"],
        "name": "Chapters: Home",
        "spoken": "25:00 of 50:00, Home",
    }
    assert out["stillSpoken"] == out["after"]["spoken"]


def test_a_new_quality_that_comes_without_chapters_keeps_those_known(markup):
    assert run(markup, "chapters")["kept"] == [5, 3]


def test_a_single_chapter_is_not_shown(markup):
    assert run(markup, "chapters_one") == {"hidden": True, "gaps": 0, "title": True}


def test_the_intro_and_the_credits_can_be_skipped_by_button_and_by_key(markup):
    out = run(markup, "skip")
    assert (out["before"], out["sBefore"]) == (None, 59)
    assert (out["intro"], out["sIntro"], out["introGone"]) == ("Skip intro", 150, None)
    # Nothing left worth skipping.
    assert out["introLastSecond"] is None
    assert (out["credits"], out["creditsTo"], out["href"]) == (
        "Skip credits",
        2450,
        "/watch/play/1",
    )


def test_skip_never_acts_by_itself_or_takes_the_focus(markup):
    out = run(markup, "skip")
    assert (out["plays"], out["focusKept"]) == (61, True)
    # The button goes away under the focus: it moves to Play.
    assert out["focusAfter"] is True


def test_the_skip_button_goes_quiet_after_a_while(markup):
    out = run(markup, "skip")
    assert (out["quietEarly"], out["quiet"]) == (False, True)


def test_the_last_credits_lead_to_the_next_episode_with_this_one_at_its_end(markup):
    out = run(markup, "skip")
    assert (out["last"], out["hrefAfter"]) == ("Next episode", "/watch/play/502")
    assert out["sInMenu"] == "/watch/play/1"
    # Saved as watched with the stop; the page left after it says nothing of the place,
    # and coming back to it starts where it was left.
    assert (out["saved"], out["beacons"], out["backAt"]) == ([3000], 0, 2710)


def test_the_last_credits_with_nothing_next_go_to_the_end(last_episode):
    out = run(last_episode, "skip_last")
    assert out == {"label": "Skip credits", "at": 2999, "hidden": True, "href": "/watch/play/1"}


def test_an_error_hides_the_skip_button(markup):
    assert run(markup, "skip_error") == {"shown": True, "underError": False}


def test_a_seek_while_a_quality_starts_is_kept_when_the_server_refuses_it(markup):
    out = run(markup, "seek_then_refused")
    assert out == {"starts": ["auto", "low", "auto"], "at": 50}


def test_a_double_click_for_full_screen_leaves_the_play_state_alone(markup):
    out = run(markup, "double_click")
    assert out["playing"] is False
    assert out["menu"] == {"open": "false", "paused": False}
    # The next single click still pauses.
    assert (out["paused"], out["stillPaused"]) == (True, True)


def test_a_click_on_the_picture_under_an_error_does_not_try_again(markup):
    out = run(markup, "double_click")
    assert (out["error"], out["retries"]) == (True, 0)
    assert out["wantsToPlay"]["busy"] == "true"


def _episode(n: int, **changes: Any) -> dict[str, Any]:
    return {
        **EPISODE,
        "item": {
            "rating_key": str(500 + n),
            "title": "The Long Coast",
            "subtitle": f"S2 · E{n}",
            "duration_s": 2700.0 + n,
        },
        "next": {"href": f"/watch/play/{501 + n}", "label": f"S2 · E{n + 1}"},
        "back_href": f"/watch/show/77?season={n}",
        "show_href": f"/watch/show/77?season={n}",
        **changes,
    }


@pytest.fixture(scope="module")
def season(tmp_path_factory) -> dict[str, Path]:
    """The pages of the episodes that follow the one open, by address."""
    changes = {3: {"leaving": {"days": 2, "keep_id": "88"}}}
    return {
        f"/watch/play/{500 + n}": _markup(tmp_path_factory, _episode(n, **changes.get(n, {})))
        for n in range(2, 24)
    }


@pytest.fixture(scope="module")
def unknown_next(tmp_path_factory) -> Path:
    return _markup(tmp_path_factory, {**EPISODE, "next": None, "next_state": "unknown"})


@pytest.fixture(scope="module")
def in_place(markup, season) -> Any:
    return run(markup, "next_in_place", saved={"mediaman.watch.quality": "low"}, pages=season)


def test_the_next_episode_takes_over_in_the_same_page_and_video(in_place):
    assert (in_place["href"], in_place["urls"]) == ("/watch/play/1", ["/watch/play/502"])
    assert (in_place["sameVideo"], in_place["volume"], in_place["oldDestroyed"]) == (
        True,
        0.4,
        True,
    )
    assert (in_place["start"]["rating_key"], in_place["start"]["quality"]) == ("502", "low")


def test_the_old_stream_is_saved_and_stopped_before_the_new_one_starts(in_place):
    assert in_place["early"] == 0
    stop, page, start = in_place["order"]
    assert (stop.endswith("/stop"), page, start) == (
        True,
        "/watch/play/502",
        "/api/watch/stream",
    )
    assert in_place["stop"]["position_s"] == 2997


def test_the_page_shows_what_the_next_episode_s_own_page_says(in_place):
    assert (in_place["title"], in_place["heading"], in_place["subtitle"]) == (
        "The Long Coast · Mediaman",
        "The Long Coast",
        "S2 · E2",
    )
    assert in_place["media"] == {"title": "S2 · E2", "artist": "The Long Coast"}
    assert (in_place["nextHref"], in_place["max"]) == ("/watch/play/503", "2702")
    assert in_place["back"] == "/watch/show/77?season=2"


def test_nothing_of_the_old_episode_is_left(in_place):
    assert in_place["before"] == {"chapters": True, "marks": 0, "menus": 0}
    assert (in_place["card"], in_place["keep"]) == (True, False)


def test_the_last_frame_stays_until_the_next_episode_plays(in_place):
    assert (in_place["held"]["shown"], in_place["left"]) == (True, False)


def test_only_the_new_episode_is_saved_after_the_change_with_its_own_played_time(in_place):
    saves = in_place["saves"]
    assert saves
    assert {s["stream_id"] for s in saves} == {"a2"}
    assert {s["played_s"] for s in saves} == {2}


def test_the_next_link_and_the_card_go_on_in_place_once_each(markup, season):
    out = run(markup, "next_by_hand", pages=season)
    assert (out["newTabKept"], out["stays"], out["cardLeaves"]) == (True, True, True)
    # The link under the keyboard is the new episode's, and still has the focus.
    assert out["first"] == {
        "pages": ["/watch/play/502"],
        "focus": "/watch/play/503",
        "urls": ["/watch/play/502"],
    }
    assert out["urls"] == ["/watch/play/502", "/watch/play/503"]


def test_the_next_episode_s_own_keep_works(markup, season):
    out = run(markup, "next_by_hand", pages=season)
    assert out["kept"] == [{"media_item_id": "88", "days": 30}]


@pytest.mark.parametrize(("where", "focus"), [("here", "keep"), ("next", "play")])
def test_undoing_a_keep_puts_the_focus_where_it_can_be(markup, season, where, focus):
    pages = {**season, "/plain": season["/watch/play/504"]}
    out = run(markup, "keep_undone", plays=where, pages=pages)
    assert (out["focus"], out["undone"]) == (focus, ["88"])


@pytest.mark.parametrize(
    ("reply", "undo"), [({"undo": True}, True), ({}, True), ({"undo": False}, False)]
)
def test_a_keep_offers_undo_unless_the_server_says_there_is_none(season, reply, undo):
    out = run(season["/watch/play/503"], "keep_offers", plays=json.dumps(reply))
    assert out == {"pill": "Kept for 7 days", "said": "Kept until", "undo": undo}


def test_a_keep_says_for_how_long_on_its_pill_and_until_when_in_the_toast(markup, season):
    pages = {**season, "/plain": season["/watch/play/504"]}
    pill, toast = run(markup, "keep_undone", plays="here", pages=pages)["said"]
    assert pill == "Kept for 7 days"
    assert re.fullmatch(r"Kept until [78] Jan 1970", toast)


@pytest.mark.parametrize("answer", ["redirect", "other"])
def test_a_page_that_cannot_take_over_is_opened_with_the_old_stream_stopped(markup, answer):
    out = run(markup, "next_falls_back", plays=answer)
    assert out == {"href": "/watch/play/502", "starts": 1, "stops": 1, "beacons": 0}


@pytest.mark.parametrize("answer", ["offline", "hangs", "refused", "unopened", "plex_down"])
def test_a_next_episode_that_cannot_be_reached_is_said_and_can_be_tried_again(
    markup, season, answer
):
    out = run(markup, "next_unreached", plays=answer, pages=season)
    assert out["message"] == {"retry": True, "focus": True}
    # A page that nothing answered for is waited for first, and is the connection's fault.
    lost = answer in ("offline", "hangs", "refused")
    assert ("mediaman" if lost else "next episode") in out["text"]
    # The system's own player would hide the message.
    assert (out["mode"], out["loading"], out["goesOn"]) == ("inline", False, False)
    assert out["href"] == ("/watch/play/502" if answer == "unopened" else "/watch/play/1")
    assert out["trying"] is None
    assert out["went"] == {"starts": 1, "urls": ["/watch/play/502"], "key": "502", "message": None}


@pytest.mark.parametrize(
    ("there", "shown"),
    [("still", "/api/watch/thumb/502"), ("backdrop", "/api/poster/77?kind=art"), ("none", None)],
)
def test_the_card_and_the_question_carry_a_picture_of_the_next_episode_where_there_is_one(
    markup, there, shown
):
    out = run(markup, "still", plays=there)
    assert (out["before"], out.get("between")) == (None, None)
    assert (out["card"], out["ask"]) == (shown, shown)


def test_a_page_that_never_opens_leaves_this_episode_as_it_was(markup):
    out = run(markup, "next_never_opens")
    assert (out["start"]["rating_key"], out["start"]["quality"]) == ("501", "low")


def test_a_next_episode_that_is_gone_is_not_offered_again(markup):
    assert run(markup, "next_gone") == {"message": True, "retry": False, "back": True}


def test_the_next_episode_is_named_at_once_and_this_one_again_where_it_stays(markup, season):
    out = run(markup, "next_at_once", pages=season)
    asked = {"subtitle": "S2 · E5 — High Tide", "switching": True, "held": True}
    assert out["asked"] == asked
    assert (out["unreached"]["subtitle"], out["left"]) == ("S2 · E4", "S2 · E4")
    assert (out["there"]["subtitle"], out["there"]["held"]) == ("S2 · E2", False)
    # A new quality of the same video holds its frame as it is.
    assert out["quality"] == {"subtitle": "S2 · E2", "switching": False, "held": True}


def test_the_new_stream_waits_for_the_old_to_stop_but_not_for_ever(markup, season):
    out = run(markup, "next_waits", pages=season)
    assert (out["before"], out["whileWaiting"], out["after"]) == (1, 1, ["auto", "low"])
    assert out["last"] == 3


def test_a_place_that_could_not_be_saved_at_the_change_over_is_sent_again(markup, season):
    out = run(markup, "next_unsaved", pages=season)
    stop = "/api/watch/stream/s1/stop"
    assert out["failed"] == [[stop, 120]]
    # Before the next episode is asked for, and taken, so not sent a third time.
    assert out["retried"][:2] == [stop, "/watch/play/502"]
    assert (out["body"], out["again"]) == ([stop, 120], 0)
    assert stop not in out["left"]


def test_a_place_that_could_not_be_saved_at_the_change_over_is_sent_as_the_page_is_left(markup):
    out = run(markup, "next_unsaved", plays="leaves")
    assert out["left"] == [["/api/watch/stream/s1/stop", 120]]


def test_a_stop_that_was_not_taken_is_sent_again_with_the_try_again(markup):
    out = run(markup, "fail_resent")
    [missed] = out["missed"]
    assert out["tried"] == [missed, "/api/watch/stream"]
    assert missed not in out["left"]


def test_an_old_place_never_goes_out_over_a_newer_one(markup):
    # The stream that failed is stopped without it.
    assert run(markup, "fail_unsaved") == [None, 420]


@pytest.mark.parametrize("subtitle", ["S2 · E5 — High Tide", "S2 · E5 — High Water"])
def test_the_next_episodes_title_is_said_once(markup, tmp_path_factory, subtitle):
    item = {**EPISODE["item"], "rating_key": "502", "subtitle": subtitle}
    page = _markup(tmp_path_factory, {**EPISODE, "item": item})
    out = run(markup, "next_said", pages={"/watch/play/502": page})
    assert out["item"] == "502"
    label = EPISODE["next"]["label"]
    # One renamed meanwhile is put right.
    again = [] if subtitle == label else [EPISODE["item"]["title"], subtitle]
    assert out["said"] == [label, *again]
    assert subtitle in out["shown"]


def test_what_is_pressed_while_the_next_episode_is_on_its_way_holds_for_it(markup, season):
    out = run(markup, "next_waits", pages=season)
    assert (out["paused"], out["plays"]) == ("Play", 1)


def test_a_page_left_on_the_way_starts_nothing_and_still_works_when_back(markup, season):
    out = run(markup, "next_left", pages=season)
    assert (out["left"], out["starts"], out["back"]) == ("/watch/play/1", 1, "501")
    assert out["again"] == 2


def test_nothing_held_on_the_bar_reaches_the_next_episode(markup, season):
    out = run(markup, "next_over_bar", pages=season)
    # A frame of the old episode that comes late is not shown as the new one's.
    assert (out["bubble"], out["asked"]) == (True, 2)
    assert out["startsAt"] == [0, None]


def test_automatic_goes_on_from_the_step_it_had_come_to(markup, season):
    out = run(markup, "next_stepped", pages=season)
    # Down for this episode, so down for the next, and up again when that plays well.
    assert out["starts"] == ["auto", "720p-4000", "medium", "medium", "1080p-8000"]
    assert (out["ticked"], out["pill"]) == (["auto"], "Auto · 720p")


def test_an_original_that_could_not_play_is_tried_anew_for_the_next_episode(markup, season):
    saved = {"mediaman.watch.quality": "original"}
    out = run(markup, "next_original", saved=saved, pages=season)
    assert out["starts"] == ["original", "1080p-10000", "original"]


@pytest.mark.parametrize(
    ("then", "at", "paused"), [("plays", 120, False), ("paused", 120, True), ("seeks", 130, False)]
)
def test_a_picture_that_fails_and_is_mended_goes_on_from_its_place(markup, then, at, paused):
    out = run(markup, "media_error", plays=then)
    assert (out["at"], out["paused"], out["starts"]) == (at, paused, 1)
    # 0:00 is never saved over the place.
    assert set(out["saved"]) <= {120}
    assert out["left"] == [None if paused else at]
    assert (out["held"], out["heldAfter"]) == (True, False)


def test_the_next_link_leaves_an_error_behind(markup):
    assert run(markup, "next_after_error") == {"error": True, "after": False}


def test_the_countdown_goes_by_the_clock(markup, season):
    assert run(markup, "countdown_clock", pages=season) == {"left": "1", "went": True}


def test_it_asks_after_three_episodes_that_began_by_themselves(markup, season):
    out = run(markup, "ask", pages=season)
    # One more after the six signs of somebody, with two episodes after each: a key, a press,
    # and from outside the page a pause, play, the volume and a jump.
    assert out["asked"] == [False] * 18 + [True, False]


def test_the_question_lets_go_of_the_stream_and_comes_out_of_the_system_s_player(markup, season):
    out = run(markup, "ask", pages=season)
    assert (out["stopped"], out["started"], out["beacons"]) == (1, 0, 0)
    assert (out["modes"], out["heldBehind"], out["focus"]) == (["inline"], True, True)
    assert (out["line"], out["fullscreens"], out["skip"]) == ("Next: S2 · E20", 0, True)


def test_tab_goes_between_the_two_choices_of_the_question(markup, season):
    assert run(markup, "ask", pages=season)["tabs"] == ["back", "continue", "back"]


def test_a_page_come_back_to_with_the_question_open_starts_nothing(markup, season):
    assert run(markup, "ask", pages=season)["startedByBack"] == 0


def test_continue_watching_goes_on_in_place(markup, season):
    out = run(markup, "ask", pages=season)
    assert (out["open"], out["urlsAfter"] - out["urls"]) == (False, 1)
    assert out["cardBehind"] is False


def test_it_asks_when_nobody_was_there_for_two_hours_and_escape_goes_to_the_details(markup):
    out = run(markup, "ask_hours")
    assert (out["atEnd"], out["afterCountdown"]) == (False, True)
    assert out["href"] == "/watch/show/77"


def test_a_stream_that_drops_and_comes_back_is_no_sign_of_somebody(markup):
    out = run(markup, "ask_restarted", plays="restart")
    assert out == {"at": 1200, "asked": True}


@pytest.mark.parametrize(("seconds", "asked"), [("1.5", True), ("2.5", False), ("-2.5", False)])
def test_a_jump_is_a_sign_of_somebody_and_a_nudge_is_not(markup, seconds, asked):
    assert run(markup, "ask_restarted", plays=seconds) == {"asked": asked}


def test_a_question_the_browser_closes_leaves_the_card_as_a_way_on(markup, season):
    out = run(markup, "ask_closed", pages=season)
    assert out["staysOpen"] is True
    # Escape said to stop: nothing begins by itself after it.
    assert out["card"] == {"shown": True, "counts": False}
    assert (out["skip"], out["wentByItself"]) == (False, False)
    assert (out["replays"], out["went"]) == (1, ["/watch/play/502"])


@pytest.mark.parametrize(("way", "button"), [("off", [1, 0]), ("question", [0, 1])])
def test_space_at_a_waiting_card_does_not_play_the_episode_again_but_play_does(markup, way, button):
    out = run(markup, "upnext_waits", plays=way)
    assert (out["card"], out["space"]) == (True, [0, 0])
    assert out["button"] == button


def test_the_second_press_of_a_double_one_on_the_card_does_nothing_to_the_next_episode(
    markup, season
):
    out = run(markup, "next_pressed_twice", pages=season)
    assert out["double"] == {"plays": True, "fullscreens": 0}
    assert (out["doublePaused"], out["own"], out["across"]) == (False, 1, True)


def test_a_key_held_on_continue_does_not_pause_the_next_episode(markup, season):
    out = run(markup, "next_pressed_twice", pages=season)
    assert (out["asked"], out["focus"], out["held"]) == (True, True, True)
    # A press of its own still pauses, and a held Space does so once.
    assert (out["enter"], out["space"]) == (False, False)


def test_a_held_enter_goes_on_skipping_but_plays_or_pauses_once_and_presses_nothing_else(markup):
    assert run(markup, "key_held") == {
        "skipped": 20,
        "paused": True,
        "back": True,
        "muted": False,
        "card": False,
    }


@pytest.mark.parametrize(("goes_on", "asked"), [(True, True), (False, False)])
def test_only_a_page_opened_by_itself_carries_the_count_on(markup, goes_on, asked):
    held = json.dumps({"count": 3, "lastActivity": 0, "goes_on": goes_on})
    out = run(markup, "ask_carried", saved={"mediaman.watch.sitting": held})
    assert out == {"asked": asked, "goesOn": False}


def test_the_count_is_handed_on_to_a_page_opened_by_itself_alone(markup):
    out = run(markup, "ask_handed_on")
    assert (out["byItself"], out["byHand"]) == ([1, True], False)


@pytest.mark.parametrize(("goes_on", "asked"), [(True, 30), (False, 0)])
def test_only_a_page_opened_by_itself_was_asked_when_the_last_one_was(markup, goes_on, asked):
    held = {"count": 1, "lastActivity": 0, "goes_on": goes_on, "pressed": -30000}
    saved = {"mediaman.watch.sitting": json.dumps(held)}
    assert run(markup, "asked_at_first", saved=saved) == asked


def test_a_page_opened_by_itself_is_told_when_the_last_one_was_asked(markup):
    assert run(markup, "ask_handed_on")["pressed"] == 0


def test_leaving_full_screen_cancels_the_countdown(markup, season):
    out = run(markup, "countdown_fullscreen", pages=season)
    assert out["entered"] == {"counts": True, "leaving": False}
    assert out["left"] == {"counts": False, "leaving": True}
    assert out["went"] is False


def test_the_card_is_said_once_and_its_countdown_never_ticks_aloud(markup):
    out = run(markup, "upnext_spoken")
    # In full screen the browser keeps Escape for itself.
    said = "Next episode in 10 seconds. Space or Escape cancels."
    assert (out["polite"], out["before"], out["cancelled"]) == ("status", "", "")
    assert (out["counting"], out["later"], out["again"]) == (said, said, said)
    # Nothing begins by itself then, so there is nothing to cancel.
    assert out["still"] == "Next episode: S2 · E5 — High Tide"


def test_autoplay_off_shows_the_next_episode_and_waits(markup):
    out = run(markup, "autoplay_off")
    assert (out["start"], out["off"], out["saved"]) == ("true", "false", "false")
    assert out["menuOpen"] == "true"
    assert out["card"] == {"hidden": False, "counts": False, "play": "Play"}
    assert (out["went"], out["wentLater"]) == (False, False)


def test_autoplay_turned_on_and_off_over_the_card_starts_and_stops_the_countdown(markup):
    out = run(markup, "autoplay_off")
    assert out["on"] == {"hidden": False, "counts": True, "play": "Pause"}
    assert out["offAgain"] == {"hidden": False, "counts": False, "play": "Play"}


def test_autoplay_stays_in_reach_under_qualities_that_scroll(markup):
    out = run(markup, "menu_parts")
    assert (out["heading"], out["group"]) == ("Quality", "Quality")
    assert (out["scrolls"], out["autoplayScrolls"]) == (3, False)


def test_a_menu_fades_at_the_ends_that_have_more(markup):
    out = run(markup, "menu_parts")
    assert (out["fits"], out["top"]) == ([], ["below"])
    assert (out["middle"], out["end"]) == (["above", "below"], ["above"])


def test_the_autoplay_choice_is_kept_and_a_film_has_none(markup, tmp_path_factory):
    out = run(markup, "autoplay_saved", saved={"mediaman.watch.autoplay": "false"})
    assert out == {"checked": "false"}
    film = _markup(tmp_path_factory, {**EPISODE, "next": None, "show_href": None})
    assert run(film, "autoplay_film") == {"row": False}


def test_a_next_episode_that_is_unknown_is_asked_for_again_and_said(unknown_next, markup):
    pages = {"/watch/play/1": unknown_next, "/found": markup}
    out = run(unknown_next, "next_unknown", pages=pages)
    assert (out["link"], out["message"], out["retry"]) == (False, True, True)
    assert "next episode" in out["text"]
    # The episode itself still plays: moving within it takes the message away.
    assert (out["streams"], out["afterSeek"]) == (0, False)
    assert out["found"] == {
        "message": False,
        "card": False,
        "link": "/watch/play/502",
        "starts": 1,
        "href": "/watch/play/1",
    }


def test_a_place_already_saved_is_not_saved_again(markup, season):
    out = run(markup, "saved_once", pages=season)
    assert (out["afterPause"], out["afterMore"]) == (0, 1)
    assert (out["stop"].endswith("/stop"), out["body"]) == (True, None)
    assert (out["after"], out["same"]) == (1, True)


def test_back_steps_through_the_history_only_where_the_app_led_here(markup) -> None:
    out = run(markup, "back")
    through = {"through": 1, "link": False, "late": "/watch/show/77"}
    link = {"through": 0, "link": True, "late": "/watch/play/1"}
    assert out == {
        "fromWatch": through,
        "reload": through,
        "newTab": link,
        "deep": link,
        "otherSite": link,
        "otherPage": link,
        "slow": {**through, "late": "/watch/play/1"},
        "slower": {**through, "late": "/watch/play/1"},
        "twice": {**through, "second": "/watch/play/1"},
        "again": {**through, "second": "/watch/show/77"},
        "onItsWay": {**through, "second": "/watch/play/1", "late": "/watch/play/1"},
        "newTabPress": link,
    }


@pytest.mark.parametrize(
    ("back_href", "through"), [("/watch/show/77", 1), ("/watch/show/77?season=3", 0)]
)
def test_back_after_the_next_episode_steps_through_unless_it_leads_to_another_season(
    tmp_path_factory, markup, back_href, through
) -> None:
    second = _markup(tmp_path_factory, {**EPISODE, "back_href": back_href})
    out = run(markup, "backAfterNext", pages={"/watch/play/502": second})
    assert out["at"] == ["/watch/play/502"]
    assert out["state"] == {"mmFrom": "/watch/show/77"}
    assert (out["through"], out["href"]) == (through, back_href)


def test_a_film_that_ends_on_a_page_that_came_by_a_link_stays_in_the_app(tmp_path_factory) -> None:
    film = {**EPISODE, "next": None, "show_href": None, "back_href": "/watch/movie/7"}
    out = run(_markup(tmp_path_factory, film), "endOfDeepLink")
    assert out == {"through": 0, "at": "/watch/movie/7"}


LOST = {"text": "Can't reach mediaman. Check your connection.", "button": "Try again"}
BUSY = "The server is busy right now. Try again in a moment."
NOTICE_LOST = "Connection lost. Trying again…"
REPLACED = {"text": "Playing somewhere else.", "button": "Play here"}


def test_a_held_arrow_moves_the_bar_at_once_and_the_video_once_when_it_is_let_go(markup):
    out = run(markup, "hold_arrow")
    assert out["held"] == {"time": "2:30", "now": "150", "left": "5%", "seeks": []}
    assert (out["early"], out["landed"], out["later"]) == ([], [150], 1)


@pytest.mark.parametrize("pointer", ["mouse", "touch"])
def test_skip_buttons_pressed_in_quick_succession_move_the_video_once(markup, pointer):
    assert run(markup, "hold_arrow")[pointer] == {"now": "620", "seeks": [620]}


def test_every_key_held_on_the_bar_moves_the_video_once(markup):
    out = run(markup, "hold_arrow")
    assert [out[k] for k in ("PageUp", "ArrowUp", "Home", "End")] == [[840], [640], [0], [2999]]


def test_presses_past_the_ends_of_the_video_stop_there_and_lose_none_after(markup):
    out = run(markup, "hold_ends")
    # 2970 is the scenario putting the video near its end.
    assert (out["start"], out["end"]) == ("0", "2999")
    assert out["seeks"] == [0, 10, 2970, 2999]


def test_presses_back_as_the_video_ends_win_over_its_end(markup):
    out = run(markup, "ends_under_press", plays="ArrowLeft")
    assert out == {"card": False, "seeks": [2985], "from": 2995, "after": False}


def test_presses_on_past_the_end_do_not_take_the_end_back(markup):
    out = run(markup, "ends_under_press", plays="ArrowRight")
    assert out == {"card": True, "seeks": [], "from": 2995, "after": True}


def test_a_press_still_to_land_is_saved_started_from_and_skipped_from(markup):
    out = run(markup, "press_not_lost")
    assert (out["saved"], out["startsAt"], out["again"]) == (123, 123, [])
    assert (out["offered"], out["skipped"], out["left"]) == ("Skip intro", [0], 10)


def test_presses_while_the_video_buffers_land_and_space_lands_one_at_once(markup):
    out = run(markup, "press_while_buffering")
    assert (out["now"], out["landed"], out["bySpace"]) == ("120", [120], [120, 130])
    assert (out["all"], out["paused"]) == ([120, 130], True)


def test_a_seek_to_what_has_not_loaded_keeps_the_frame_until_it_is_there(markup):
    out = run(markup, "seek_holds")
    assert out == {"loaded": False, "notLoaded": True, "seeked": True, "seen": False}


def test_a_segment_the_server_is_too_busy_for_is_asked_for_again_when_it_says(markup):
    out = run(markup, "busy", plays="fragLoadError")
    assert (out["early"], out["stopped"]) == ([0, 1], True)
    assert (out["after"]["loads"], out["after"]["streams"]) == ([-1], 1)
    assert (out["starts"], out["stops"], out["message"], out["notice"]) == (1, 0, None, False)


def test_a_playlist_the_server_is_too_busy_for_is_asked_for_again_when_it_says(markup):
    out = run(markup, "busy", plays="manifestLoadError")
    assert (out["early"], out["stopped"]) == ([0, 1], True)
    assert (out["after"]["streams"], out["after"]["src"]) == (2, "/p1")
    assert out["after"]["startPosition"] == 100
    assert (out["starts"], out["stops"], out["message"], out["notice"]) == (1, 0, None, False)


@pytest.mark.parametrize("how", ["offline", "gateway"])
def test_a_short_drop_keeps_the_stream_and_is_tried_again_with_longer_waits(markup, how):
    out = run(markup, "dropped", plays=how)
    assert (out["stopped"], out["triedAt"]) == (True, [1, 3, 7, 15, 23, 31, 39])
    trying = {"message": None, "destroyed": False, "notice": True, "loading": True}
    assert out["whileTrying"] == trying
    assert out["back"] == {"notice": False, "message": None}
    assert (out["starts"], out["later"]) == (1, None)


def test_a_drop_is_said_only_once_the_picture_waits_for_it_and_to_a_screen_reader(markup):
    out = run(markup, "dropped", plays="offline")
    assert (out["whilePlaying"], out["atFirst"], out["waiting"]) == (False, False, True)
    assert (out["role"], "Connection lost" in out["text"]) == ("status", True)


def test_a_drop_that_lasts_about_a_minute_is_said_with_the_frame_kept(markup):
    out = run(markup, "dropped_long")
    assert (out["before"], out["message"], out["notice"]) == (None, LOST, False)
    assert (out["destroyed"], out["mode"], out["triesUnderMessage"]) == (False, "inline", 0)


def test_try_again_and_coming_online_carry_on_with_the_same_stream(markup):
    out = run(markup, "dropped_long")
    assert out["again"] == {"loads": 1, "message": None, "notice": True}
    assert out["messageAgain"] == LOST
    assert out["online"] == {"loads": 1, "message": None}
    assert (out["back"], out["starts"]) == (None, 1)


def test_a_drop_is_no_error_while_there_is_video_left_to_play(markup):
    out = run(markup, "dropped_playing_on")
    assert out == {"playing": None, "waiting": None, "late": True, "starts": 1}


def test_leaving_during_a_drop_saves_the_place_reached(markup):
    assert run(markup, "dropped_left") == [125]


def test_coming_online_tries_at_once_and_never_plays_what_the_person_paused(markup):
    out = run(markup, "dropped_online")
    assert (out["atOnce"], out["message"], out["after"]) == (1, True, None)
    assert (out["plays"], out["paused"]) == (0, True)


@pytest.mark.parametrize("way", ["message", "asleep", "awake"])
def test_a_connection_back_after_hours_waits_for_a_press(markup, way):
    out = run(markup, "dropped_hours", plays=way)
    assert (out["tried"], out["message"]) == (0, LOST)
    assert out["byPress"] == {"tried": 1, "message": None, "paused": False}
    assert out["triedOn"] == 2


def test_what_comes_by_itself_after_hours_is_paused(markup):
    out = run(markup, "dropped_hours", plays="under way")
    assert out == {"tried": 0, "message": None, "paused": True, "play": "Play"}


def test_a_next_episode_never_begins_by_itself_once_the_connection_is_back_after_hours(
    markup, season
):
    out = run(markup, "next_dropped_hours", pages=season)
    assert out == {"starts": 1, "message": LOST, "byPress": 2}


@pytest.mark.parametrize("how", ["offline", "gateway"])
def test_a_start_that_does_not_reach_mediaman_is_tried_again_where_it_stood(markup, how):
    out = run(markup, "start_dropped", plays=how)
    assert out["first"] == {"message": None, "held": True, "notice": True}
    assert (3 <= out["tries"] <= 8, out["waiting"], out["at"]) == (True, None, 105)
    assert out["back"] == {"message": None, "notice": False, "quality": None, "toasts": 0}
    assert (out["late"], out["frame"]) == (LOST, True)
    assert out["online"] == {"starts": 1, "message": None, "notice": False}


def test_a_next_episode_is_waited_for_through_a_short_drop(markup, season):
    out = run(markup, "next_dropped", plays="short", pages=season)
    assert out["first"] == {"message": None, "notice": True, "held": True}
    assert out["went"] == {"key": "502", "message": None, "notice": False}


def test_a_next_episode_out_of_reach_is_said_and_goes_on_by_itself_once_online(markup, season):
    out = run(markup, "next_dropped", plays="long", pages=season)
    assert (out["late"], out["subtitle"]) == (LOST, "S2 · E4")
    assert out["asked"] > 4
    assert out["went"] == {"key": "502", "message": None, "notice": False}


def test_what_follows_is_asked_again_through_a_drop_and_once_online(unknown_next, markup):
    out = run(unknown_next, "next_unknown_dropped", pages={"/found": markup})
    assert (out["first"], out["notice"], out["late"]) == (None, True, LOST)
    assert out["found"] == {"message": None, "card": True}


def test_a_slow_try_that_fails_once_what_follows_is_known_changes_nothing(unknown_next, markup):
    out = run(unknown_next, "next_unknown_twice", pages={"/found": markup})
    assert out == {"card": True, "tries": 2, "left": "6"}


def test_moving_back_while_what_follows_is_asked_ends_the_asking(unknown_next):
    out = run(unknown_next, "next_unknown_left")
    assert out == {"message": None, "notice": False, "asked": 1}


@pytest.mark.parametrize("way", ["fragLoadError", "manifestLoadError", "save"])
def test_a_title_started_somewhere_else_stops_here_for_good(markup, way):
    out = run(markup, "replaced", plays=way)
    assert (out["message"], out["role"], out["focus"]) == (REPLACED, "alert", True)
    assert (out["frame"], out["playing"], out["destroyed"]) == (True, False, True)
    # Nothing is saved over the place the other one is at, and nothing starts by itself.
    assert (out["told"], out["byItself"], out["still"]) == (0, 0, REPLACED)
    assert out["here"] == {"starts": 1, "at": 105, "message": None}
    assert out["button"] == "Try again"


def test_play_here_goes_on_from_where_the_other_one_got_to(markup, tmp_path_factory):
    # Played there for less than is resumed, or past the watched mark, it has no start of its own.
    elsewhere = _markup(tmp_path_factory, {**EPISODE, "start_s": 0, "place_s": 900.5})
    out = run(markup, "replaced", plays="fragLoadError", pages={"/elsewhere": elsewhere})
    assert out["here"] == {"starts": 1, "at": 900.5, "message": None}


def test_play_here_goes_on_from_this_tab_s_place_where_none_is_saved(markup, tmp_path_factory):
    elsewhere = _markup(tmp_path_factory, {**EPISODE, "start_s": 0, "place_s": None})
    out = run(markup, "replaced", plays="fragLoadError", pages={"/elsewhere": elsewhere})
    assert out["here"] == {"starts": 1, "at": 105, "message": None}


@pytest.mark.parametrize(
    ("way", "place", "at"),
    [
        # What the page loaded with is of an older viewing, and what this tab saved is its own.
        ("save", 2900.5, [110]),
        ("save", 105.0, [110]),
        ("beacon", 105.0, [115]),
        ("beacon", 110.0, [115]),
        ("save", 900.5, [900.5]),
        # Finished there, past what this one can start at.
        ("save", 3000.0, [2999]),
        ("twice", 900.5, [900.5, 905.5]),
    ],
)
def test_play_here_takes_only_a_place_the_other_one_saved(tmp_path_factory, way, place, at):
    here = _markup(tmp_path_factory, {**EPISODE, "place_s": 2900.5})
    elsewhere = _markup(tmp_path_factory, {**EPISODE, "place_s": place})
    out = run(here, "replaced_after_save", plays=way, pages={"/elsewhere": elsewhere})
    assert out == at


@pytest.mark.parametrize(
    ("follows", "place", "at"),
    [
        # Nothing follows: from the end it would leave the player at once.
        (None, 3000.0, 110),
        (None, 2999.5, 110),
        (None, 2990.0, 2990),
        ({"href": "/watch/play/502", "label": "S2 · E5"}, 3000.0, 2999),
    ],
)
def test_play_here_keeps_this_tab_s_place_where_the_title_ended_there_and_nothing_follows(
    tmp_path_factory, follows, place, at
):
    here = _markup(tmp_path_factory, {**EPISODE, "next": follows})
    elsewhere = _markup(tmp_path_factory, {**EPISODE, "next": follows, "place_s": place})
    out = run(here, "replaced_after_save", plays="save", pages={"/elsewhere": elsewhere})
    assert out == [at]


def test_a_press_as_play_here_asks_for_the_place_starts_nothing_from_this_one(
    markup, tmp_path_factory
):
    elsewhere = _markup(tmp_path_factory, {**EPISODE, "place_s": 900.5})
    out = run(markup, "replaced_pick", plays="pressed", pages={"/elsewhere": elsewhere})
    assert (len(out["starts"]), out["at"]) == (1, 900.5)


@pytest.mark.parametrize(
    ("picked", "started"), [("same", ["best"]), ("once", ["medium"]), ("twice", ["low"])]
)
def test_a_quality_picked_under_playing_somewhere_else_is_play_here_with_it(
    markup, tmp_path_factory, picked, started
):
    elsewhere = _markup(tmp_path_factory, {**EPISODE, "place_s": 900.5})
    saved = {"mediaman.watch.quality": "best"}
    out = run(markup, "replaced_pick", plays=picked, saved=saved, pages={"/elsewhere": elsewhere})
    assert out == {"starts": started, "at": 900.5, "message": None}


def test_a_stream_this_page_replaced_is_never_taken_for_another_page_s(markup):
    assert run(markup, "replaced_by_itself", plays="save") == {"message": None, "starts": 2}
    out = run(markup, "replaced_by_itself", plays="start")
    assert (out["message"], out["starts"]) == (None, 4)
    assert out["muchLater"] == {"message": None, "starts": 5}
    assert (out["later"], out["after"]) == (REPLACED["text"], 5)


@pytest.mark.parametrize("way", ["segment", "save", "never", "asked before"])
def test_a_title_started_somewhere_else_after_a_quick_pick_stops_here_at_any_moment(markup, way):
    out = run(markup, "replaced_long_after", plays=way)
    assert out == {"message": REPLACED, "starts": 0}


@pytest.mark.parametrize(("way", "asked"), [("idle", [0, 20]), ("drop", [0, 20, 21, 21])])
def test_what_the_page_starts_by_itself_says_when_it_was_asked_and_stops_where_refused(
    markup, way, asked
):
    out = run(markup, "start_refused", plays=way)
    assert (out["message"], out["frame"], out["loading"]) == (REPLACED, True, False)
    assert (out["pages"], out["asked"]) == ([True] * len(asked), asked)
    # Nothing more by itself; the person's press is asked for now, and plays here.
    assert (out["byItself"], out["here"]) == (0, {"asked": 0, "at": 105, "message": None})


@pytest.mark.parametrize(
    ("act", "ago"),
    [
        ("quality", 0),
        ("try again after a drop", 0),
        ("play here", 0),
        # Asked as it was pressed, not once the place was known.
        ("play here, its place 15 s away", 15),
        ("try again after a refusal", 0),
        ("play with no stream", 0),
        ("play on a paused video", 0),
        ("play as a start is on its way", 0),
        ("seek by a key", 0),
        ("seek by a key, long landed", 20),
        ("seek on the bar", 0),
        ("next", 0),
        ("next as it is on its way by itself", 0),
        ("next after it was out of reach", 0),
    ],
)
def test_what_the_person_presses_is_when_the_page_was_last_asked(markup, season, act, ago):
    assert run(markup, "asked_after", plays=act, pages=season)["asked"] == ago


@pytest.mark.parametrize(
    "act",
    [
        "pause",
        "pause as a start is on its way",
        "a stream that went",
        "a start tried again by itself",
        "online as a start is lost",
        "next by itself",
        "next by itself, online after a drop",
        "next by a press, online after a drop",
        "back from the page cache",
    ],
)
def test_what_the_page_does_by_itself_is_not_a_press(markup, season, act):
    out = run(markup, "asked_after", plays=act, pages=season)
    assert out["since"] >= 20
    assert out["asked"] == out["since"]


def test_a_page_back_from_the_cache_leaves_the_title_to_the_page_asked_since(markup, season):
    out = run(markup, "asked_after", plays="back from the page cache, refused", pages=season)
    assert (out["asked"], out["message"]) == (20, REPLACED)


def test_a_start_that_is_never_answered_ends_in_a_message_and_its_late_answer_is_stopped(markup):
    out = run(markup, "start_unanswered")
    assert (out["early"], out["loading"]) == (None, False)
    assert out["message"] == {"text": "Can't play this right now.", "button": "Try again"}
    assert out["after"] == {"streams": 0, "message": None, "stopped": True}


@pytest.mark.parametrize("way", ["pressed", "unpressed"])
def test_a_try_never_answered_in_a_drop_is_stopped_or_ended_and_never_plays(markup, way):
    out = run(markup, "start_unanswered_in_drop", plays=way)
    ended = way == "pressed"
    assert (out["message"], out["ended"]) == (LOST, 1 if ended else 0)
    assert out["after"] == {"streams": 0, "stopped": not ended}


@pytest.mark.parametrize("way", ["press", "online", "early"])
@pytest.mark.parametrize("then", ["answered", "failed"])
def test_a_press_or_coming_online_drops_a_start_that_hangs_and_tries_afresh(markup, way, then):
    out = run(markup, "start_hangs_in_drop", plays=f"{way} {then}")
    assert (out["message"], out["asked"]) == (None if way == "early" else LOST, 1)
    assert out["after"] == {"streams": 0, "starts": 1, "ended": 1, "message": None}


def test_tries_that_hang_never_pile_up_and_one_ended_is_not_taken_for_another_page_s(markup):
    out = run(markup, "starts_hang")
    assert (out["pressed"], out["picked"], out["notice"]) == (1, 1, NOTICE_LOST)
    assert (out["replaced"], out["starts"]) == (None, 1)


@pytest.mark.parametrize("details", ["fragLoadError", "manifestLoadError"])
def test_a_server_too_busy_for_good_is_said_once_there_is_nothing_left_to_play(markup, details):
    out = run(markup, "busy_for_good", plays=details)
    assert (out["playing"], out["early"]) == (None, None)
    busy = {"text": BUSY, "button": "Try again"}
    assert (out["message"], out["after"]) == (busy, [0, 1])
    assert (out["again"]["message"], out["again"]["starts"]) == (None, 2)


@pytest.mark.parametrize("chosen", ["auto", "1080p-8000"])
def test_a_segment_taken_after_a_refusal_is_slow_by_the_connection_however_long(markup, chosen):
    out = run(markup, "busy_once", plays="slow", saved={"mediaman.watch.quality": chosen})
    # Automatic steps down as for any stall, ten seconds after the server took the request.
    steps = chosen == "auto"
    assert (out["early"], out["stepped"], out["message"]) == (1, 2 if steps else 1, None)
    assert out["destroyed"] is steps


def test_a_refusal_after_one_that_was_taken_is_no_stall_of_the_connection_s(markup):
    assert run(markup, "busy_once", plays="again") == {"starts": 1}


def test_a_connection_lost_after_a_refusal_is_said_as_lost_and_comes_back(markup):
    assert run(markup, "busy_once", plays="lost") == {"message": LOST, "tried": 1}


def test_refusals_on_either_side_of_a_drop_are_not_one_long_one(markup):
    out = run(markup, "busy_once", plays="lost for a while")
    assert out == {"message": None, "starts": 1}
    # Nor is one before a drop that was said, once it is tried again.
    out = run(markup, "busy_once", plays="lost for long")
    assert out == {"message": None, "starts": 1}


@pytest.mark.parametrize("first", ["refused", "lost"])
@pytest.mark.parametrize("status", [0, 502, 504])
def test_a_drop_and_a_refusal_by_turns_end_in_a_message(markup, first, status):
    out = run(markup, "busy_and_lost", plays=f"{first} {status}")
    busy = {"text": BUSY, "button": "Try again"}
    assert out["message"] in (LOST, busy)
    assert 115 <= out["at"] <= 125
    assert (out["byItself"], out["again"]) == (0, None)


@pytest.mark.parametrize("light", ["silent", "gateway"])
def test_a_drop_that_says_nothing_is_a_lost_connection_and_comes_back(markup, light):
    out = run(markup, "silent", plays=light, saved={"mediaman.watch.quality": "auto"})
    assert (out["whilePlaying"], out["waiting"]) == (None, NOTICE_LOST)
    # The stream and what it has loaded stay, and Automatic blames no quality.
    assert (out["starts"], out["startsAtEnd"], out["destroyed"]) == (1, 1, False)
    assert (out["message"], out["play"]) == (LOST, "Play")
    assert out["saves"] == 0
    assert 50 <= out["at"] <= 75
    assert out["online"] == {"tried": 1, "message": None}
    assert out["back"] is None


def test_a_server_that_answers_light_requests_is_slow_and_not_lost(markup):
    out = run(markup, "silent_then_slow", saved={"mediaman.watch.quality": "auto"})
    assert out == {"lost": NOTICE_LOST, "slow": None, "starts": 2}


@pytest.mark.parametrize("chosen", ["auto", "1080p-8000"])
@pytest.mark.parametrize("light", ["answered", "silent", "silent bytes"])
def test_a_long_stall_is_a_drop_only_where_nothing_at_all_comes(markup, chosen, light):
    plays = light if "bytes" in light else f"{light} none"
    saved = {"mediaman.watch.quality": chosen}
    out = run(markup, "stalled_silent", plays=f"{plays} 0", saved=saved)
    if light == "silent":
        assert out == {"starts": 1, "destroyed": False, "notice": NOTICE_LOST}
        return
    steps = chosen == "auto"
    assert (out["starts"], out["destroyed"], out["notice"]) == (2 if steps else 1, steps, None)


@pytest.mark.parametrize(
    "chosen", ["1080p-8000", "original", pytest.param("auto", id="lowest step")]
)
@pytest.mark.parametrize("after", [12000, 41000])
def test_a_silence_that_begins_late_in_a_stall_is_a_drop(markup, chosen, after):
    saved = {"mediaman.watch.quality": chosen}
    for light, notice in (("silent", NOTICE_LOST), ("answered", None)):
        out = run(markup, "stalled_silent", plays=f"{light} none {after}", saved=saved)
        assert out == {"starts": 1, "destroyed": False, "notice": notice}


@pytest.mark.parametrize("chosen", ["auto", "1080p-8000"])
@pytest.mark.parametrize("after", ["0 seek", "21000 seek", "0 seeks"])
def test_a_wait_after_a_seek_is_a_drop_only_where_nothing_at_all_comes(markup, chosen, after):
    saved = {"mediaman.watch.quality": chosen}
    for light, notice in (("silent", NOTICE_LOST), ("silent bytes", None), ("answered", None)):
        plays = light if "bytes" in light else f"{light} none"
        out = run(markup, "stalled_silent", plays=f"{plays} {after}", saved=saved)
        # It says nothing of the quality, whichever way.
        assert out == {"starts": 1, "destroyed": False, "notice": notice}


@pytest.mark.parametrize("way", ["refused", "lost"])
def test_a_wait_gone_silent_once_a_refusal_or_a_drop_is_over_is_a_drop(markup, way):
    out = run(markup, "seek_then_silent", plays=way)
    assert out == {"before": None, "notice": NOTICE_LOST, "starts": 1}


def test_a_wait_that_begins_before_the_video_has_moved_on_is_looked_at_too(markup):
    assert run(markup, "waits_unmoved") == {"notice": NOTICE_LOST, "starts": 1}


def test_the_light_request_carries_no_session(markup):
    assert run(markup, "light_request") == ["omit"]


@pytest.mark.parametrize(
    ("way", "then"),
    [
        ("seen", {"tried": 1, "message": None}),
        ("hidden", {"tried": 0, "message": LOST}),
        ("left", {"tried": 0, "message": LOST}),
        # Nobody may be there after hours.
        ("asleep", {"tried": 0, "message": LOST}),
    ],
)
def test_a_server_that_comes_back_unsaid_is_found_while_the_message_is_seen(markup, way, then):
    out = run(markup, "back_unsaid", plays=way)
    assert (out["message"], out["asked"]) == (LOST, 3)
    assert out["then"] == then
    if way == "hidden":
        assert (out["askedThen"], out["seen"]) == (0, {"tried": 1, "message": None})
    if way in ("left", "asleep"):
        assert out["askedThen"] == 0


def test_a_server_that_answers_and_still_plays_nothing_is_left_to_a_press_after_three_rounds(
    markup,
):
    out = run(markup, "back_but_failing")
    assert (out["rounds"], out["message"]) == (3, LOST)
    # Neither asked nor tried any more, until the person says so: then three rounds again.
    assert out["later"] == [0, 0, 0]
    assert (out["pressed"], out["after"]) == ({"tried": 1, "message": None}, 4)


def test_a_server_that_played_again_is_tried_three_rounds_after_the_next_drop(markup):
    out = run(markup, "back_but_failing", plays="plays between")
    assert (out["before"], out["played"], out["dropped"]) == (1, None, LOST)
    assert (out["rounds"], out["later"]) == (3, [0, 0, 0])


def test_once_tried_again_by_hand_the_server_is_not_asked_for_any_more(markup):
    out = run(markup, "back_unsaid", plays="again")
    assert (out["then"]["tried"], out["askedThen"]) == (1, 0)


@pytest.mark.parametrize("what", ["start", "playlist"])
def test_a_slow_start_is_said_and_waited_for(markup, what):
    out = run(markup, "hung", plays=f"{what} answered")
    assert (out["early"], out["slow"]) == (None, "Still starting…")
    assert (out["later"], out["message"]) == ("Still starting…", None)
    assert out["played"] == {"notice": None, "message": None, "starts": 2}


@pytest.mark.parametrize("what", ["start", "playlist"])
def test_a_start_that_hangs_in_a_drop_is_said_soon_and_comes_back_online(markup, what):
    out = run(markup, "hung", plays=f"{what} silent")
    assert (out["early"], out["later"]) == (None, NOTICE_LOST)
    assert out["message"] == LOST
    assert 60 <= out["at"] <= 80
    # Nothing is asked to start again and again of a server that does not answer.
    assert out["unattended"] == 0
    assert out["online"] == {"tried": True, "message": None}
    assert out["back"] is None


def test_a_start_that_hung_is_taken_when_answered_and_else_asked_for_again(markup):
    out = run(markup, "hung_back", plays="answers")
    assert out == {
        "lost": NOTICE_LOST,
        "starts": 2,
        "stream": True,
        "message": None,
        "ended": 0,
    }
    out = run(markup, "hung_back", plays="never")
    assert (out["starts"], out["stream"], out["message"], out["ended"]) == (3, True, None, 1)


def test_a_move_in_a_drop_tries_at_once_and_with_short_waits_again(markup):
    assert run(markup, "seek_in_drop") == {"atOnce": 1, "next": 1}


def test_a_move_in_a_start_that_does_not_come_through_tries_at_once_from_there(markup):
    assert run(markup, "seek_in_lost_start") == {"atOnce": 1, "at": [10]}


def test_a_busy_server_is_said_once_the_picture_has_waited_for_it_a_while(markup):
    out = run(markup, "busy_said")
    assert (out["playing"], out["early"]) == (None, None)
    assert (out["busy"], out["role"]) == ("The server is busy. Trying again…", "status")
    assert (out["taken"], out["message"]) == (None, None)


def test_a_refusal_with_much_loaded_counts_only_from_when_that_has_played(markup):
    out = run(markup, "busy_then_dry")
    assert (out["dry"], out["starts"]) == (None, 1)
    assert out["late"] == BUSY


def test_a_try_taken_and_lost_again_in_a_drop_steps_nowhere(markup):
    out = run(markup, "taken_then_lost")
    assert out == {"starts": 1, "early": None, "message": LOST}


def test_a_refusal_before_a_seek_counts_for_nothing_after_it(markup):
    out = run(markup, "busy_once", plays="back")
    assert (out["message"], out["stops"]) == (None, 0)


def test_a_paused_video_with_more_loaded_never_ends_for_a_busy_server(markup):
    assert run(markup, "busy_paused") == {"message": None, "destroyed": False}


def test_a_wait_for_a_busy_server_says_nothing_of_the_quality(markup):
    assert run(markup, "busy_stalls") == {"busy": 1, "slow": 2}


def test_a_title_started_somewhere_else_at_its_end_never_goes_on_to_the_next(markup, season):
    out = run(markup, "replaced_at_end", pages=season)
    assert out == {"message": "Playing somewhere else.", "card": False, "went": False}


def test_f_and_the_arrows_work_after_the_mouse_used_the_volume_slider(markup):
    out = run(markup, "slider_keys")
    assert out["byMouse"] == {"moved": 10, "full": 1}
    # There by Tab the arrows are the slider's own.
    assert out["byTab"] == {"moved": 0, "full": 1}


def test_sound_dragged_to_nothing_comes_back_as_loud_as_it_was(markup):
    out = run(markup, "drag_to_silence")
    assert out["silent"] == {"volume": 0.7, "muted": True, "slider": 0, "name": "Unmute"}
    assert out["saved"]["mediaman.watch.volume"] == "0.7"
    assert out["back"] == {"volume": 0.7, "muted": False, "slider": 0.7, "name": "Mute"}
    assert out["up"] == {"volume": 0.4, "muted": False, "slider": 0.4, "name": "Mute"}


MOVIE = {**EPISODE, "next": None, "show_href": None, "back_href": "/watch/movie/501"}


@pytest.fixture(scope="module")
def film(tmp_path_factory) -> Path:
    return _markup(tmp_path_factory, MOVIE)


def rating(markup: Path, what: str) -> Any:
    return run(markup, "rating", saved={}, plays=what)


def test_the_end_of_a_film_asks_once_it_is_saved_and_stays(film):
    out = rating(film, "asked")
    assert out["before"]["asks"] == 0
    ended = out["ended"]
    assert (ended["card"], ended["asks"], ended["at"]) == (True, 1, "/watch/play/1")
    assert (ended["title"], ended["said"]) == ("How was Low Water?", "How was Low Water?")
    assert (ended["thumbs"], ended["focusTaken"]) == (4, False)


def test_the_end_of_a_season_names_the_season(last_episode):
    assert rating(last_episode, "season")["ended"]["title"] == "How was season 2 of The Long Coast?"


def test_the_question_waits_for_every_save_in_flight(film):
    out = rating(film, "saving")
    assert out["saves"] == 2
    assert (out["waiting"]["asks"], out["oneLeft"]["asks"], out["ended"]["asks"]) == (0, 0, 1)


@pytest.mark.parametrize("what", ["refused", "fails", "unsaved"])
def test_with_nothing_to_ask_the_end_leads_on_at_once(film, what):
    out = rating(film, what)["ended"]
    assert (out["card"], out["at"]) == (False, "/watch/movie/501")
    assert out["asks"] == (0 if what == "unsaved" else 1)


def test_a_server_that_does_not_answer_holds_the_end_up_only_a_moment(film):
    out = rating(film, "silent")
    assert out["ended"]["at"] == "/watch/play/1"
    assert (out["after"]["card"], out["after"]["at"]) == (False, "/watch/movie/501")


@pytest.mark.parametrize("what", ["replay", "back"])
def test_going_back_or_playing_again_takes_the_question_along(film, what):
    out = rating(film, what)
    assert (out["after"]["card"], out["status"], out["late"]) == (False, "", "/watch/play/1")


def test_a_seek_back_before_the_save_is_taken_asks_nothing(film):
    out = rating(film, "backWhileAsked")
    assert (out["ended"]["card"], out["ended"]["asks"], out["late"]) == (False, 0, "/watch/play/1")


@pytest.mark.parametrize(
    "what", ["close", "escape", "rated", "ignored", "clicked", "clickedTakenBack", "keyed"]
)
def test_done_with_the_question_the_film_leads_back(film, what):
    out = rating(film, what)
    assert out.get("early", out["ended"])["at"] == "/watch/play/1"
    assert (out["after"]["card"], out["after"]["at"]) == (False, "/watch/movie/501")
    assert out.get("focus", True)


def test_a_rating_still_on_its_way_is_sent_again_to_outlive_the_page(film):
    kept = rating(film, "leave")["kept"]
    assert kept == {
        "url": "/api/watch/ratings/movie-501",
        "method": "PUT",
        "body": '{"level":2}',
        "keepalive": True,
    }


def test_a_title_that_turns_out_not_to_be_rateable_is_not_asked_about(film):
    out = rating(film, "unrateable")["ended"]
    assert (out["card"], out["said"], out["at"]) == (False, "", "/watch/movie/501")
    out = in_credits(film, "unrateable")["after"]
    assert (out["card"], out["paused"], out["at"]) == (False, False, "/watch/play/1")


def test_the_question_leaves_the_system_s_own_player(film):
    assert rating(film, "native")["mode"] == "inline"


def test_the_next_episode_comes_as_soon_with_the_question_beside_its_card(markup):
    out = rating(markup, "countdown")
    for seen in (out["ended"], out["early"]):
        assert (seen["card"], seen["upnext"]) == (True, True)
    assert out["went"] is True
    assert out["after"]["card"] is False


def test_a_rating_taken_back_on_its_way_out_is_taken_back_past_the_page(film):
    kept = rating(film, "leaveTakenBack")["kept"]
    assert (kept["method"], kept["keepalive"], kept.get("body")) == ("DELETE", True, None)


def test_a_rating_that_is_saved_is_not_sent_again_on_leaving(film):
    assert rating(film, "leaveSaved")["kept"] is None


@pytest.mark.parametrize("what", ["takenBack", "tabbed"])
def test_the_question_stays_for_a_rating_taken_back_and_for_the_keyboard_in_it(film, what):
    out = rating(film, what)["stays"]
    assert (out["card"], out["at"]) == (True, "/watch/play/1")


def test_a_message_takes_the_place_of_the_question_and_the_page_stays(film):
    out = rating(film, "message")
    assert out["message"]["text"] == "Playing somewhere else."
    assert (out["after"]["card"], out["late"]) == (False, "/watch/play/1")


def test_the_next_episode_s_question_is_not_asked_behind_picture_in_picture(markup):
    assert rating(markup, "picture")["ended"]["asks"] == 0


@pytest.mark.parametrize("what", ["next", "native"])
def test_the_next_episode_is_never_met_by_the_question(markup, what):
    out = rating(markup, what)
    assert out.get("pressed", out["after"])["card"] is False
    if what == "native":
        assert (out["ended"]["asks"], out["mode"]) == (0, "fullscreen")


def test_with_no_room_for_both_cards_the_question_waits_for_the_other_to_go(markup):
    out = rating(markup, "tightCancel")
    assert (out["ended"]["card"], out["ended"]["asks"], out["ended"]["said"]) == (False, 0, "")
    assert (out["cancelled"]["card"], out["cancelled"]["asks"]) == (True, 1)


@pytest.mark.parametrize("what", ["tightCountdown", "tightSeek"])
def test_with_no_room_the_next_episode_or_a_seek_spends_no_question(markup, what):
    assert rating(markup, what)["cancelled"]["asks"] == 0


def test_a_save_that_took_most_of_the_wait_leaves_no_time_to_spend_the_question(film):
    out = rating(film, "late")["ended"]
    assert (out["asks"], out["at"]) == (0, "/watch/movie/501")


@pytest.mark.parametrize(("what", "asks"), [("hidden", 0), ("hiddenAnswer", 1)])
def test_a_film_that_ends_in_a_hidden_page_waits_for_it_to_be_seen(film, what, asks):
    out = rating(film, what)
    assert (out["hidden"]["card"], out["hidden"]["asks"]) == (False, asks)
    assert out["hidden"]["at"] == "/watch/play/1"
    assert (out["back"]["card"], out["back"]["asks"]) == (True, 1)
    assert out["late"] == "/watch/movie/501"


def test_a_film_played_again_before_the_page_is_seen_is_not_asked_about(film):
    out = rating(film, "hiddenReplay")["back"]
    assert (out["card"], out["asks"], out["at"]) == (False, 0, "/watch/play/1")


def test_a_season_that_ends_in_a_hidden_page_asks_nothing(markup):
    out = rating(markup, "hidden")
    assert (out["hidden"]["asks"], out["back"]["asks"]) == (0, 0)


def test_escape_is_the_next_episode_s_card_s_unless_the_question_has_the_focus(markup):
    out = rating(markup, "escape")["after"]
    assert (out["card"], out["upnext"]) == (True, False)
    out = rating(markup, "escapeInCard")["after"]
    assert (out["card"], out["upnext"]) == (False, True)


def test_the_question_gives_way_to_are_you_still_watching(markup):
    out = rating(markup, "stillWatching")
    assert (out["ended"]["card"], out["after"]["card"], out["dialog"]) == (True, False, True)


def in_credits(markup: Path, what: str) -> Any:
    return run(markup, "credits", saved={}, plays=what)


def test_the_question_comes_as_the_last_credits_begin_and_the_film_plays_on(film):
    out = in_credits(film, "asked")
    assert (out["before"]["card"], out["before"]["asks"]) == (False, 0)
    seen = out["in"]
    assert (seen["card"], seen["asks"], seen["said"]) == (True, 1, "How was Low Water?")
    assert (seen["paused"], seen["at"]) == (False, "/watch/play/1")
    # The place was saved in the credits first; the person and the controls are left alone.
    assert seen["saves"][-1] >= 2700
    assert (seen["focus"], seen["quiet"], seen["beside"]) == (True, True, True)


def test_the_place_saved_as_the_credits_begin_is_never_rounded_to_before_them(film):
    assert in_credits(film, "fraction")["in"]["saves"][0] >= 2700.92


@pytest.mark.parametrize("what", ["early", "notFinal", "native", "picture"])
def test_without_credits_to_go_by_or_with_the_picture_elsewhere_the_credits_ask_nothing(film, what):
    out = in_credits(film, what)
    assert (out["in"]["card"], out["in"]["asks"]) == (False, 0)
    assert out["end"]["asks"] == 1


def test_back_from_the_system_s_own_player_the_credits_ask(film):
    out = in_credits(film, "native")
    assert (out["native"]["card"], out["after"]["card"]) == (False, True)


def test_scrubbing_over_the_credits_asks_nothing_and_playing_into_them_still_does(film):
    out = in_credits(film, "scrubbed")
    assert (out["passed"]["asks"], out["back"]["asks"]) == (0, 0)
    assert (out["after"]["card"], out["after"]["asks"]) == (True, 1)


def test_a_film_resumed_in_its_credits_is_asked_about_once_they_have_played_a_while(film):
    out = in_credits(film, "resumed")
    assert (out["passed"]["asks"], out["after"]["card"], out["after"]["asks"]) == (0, True, 1)


@pytest.mark.parametrize("what", ["rebuffer", "seekOn", "alone"])
def test_the_question_outlasts_what_happens_within_the_credits(film, what):
    after = in_credits(film, what)["after"]
    assert (after["card"], after["asks"], after["at"]) == (True, 1, "/watch/play/1")


@pytest.mark.parametrize("what", ["seekBack", "arrowBack"])
def test_going_back_before_the_credits_takes_the_question_away_for_good(film, what):
    out = in_credits(film, what)
    assert out.get("back", out["after"])["card"] is False
    assert (out["after"]["card"], out["end"]["asks"]) == (False, 1)
    assert out["end"]["at"] == "/watch/movie/501"


def test_a_question_that_comes_after_a_step_back_is_put_when_the_credits_play(film):
    out = in_credits(film, "seekWhileAsked")
    assert (out["back"]["card"], out["back"]["asks"]) == (False, 1)
    assert (out["after"]["card"], out["after"]["asks"]) == (True, 1)


def test_a_step_back_before_the_place_is_saved_spends_no_question(film):
    out = in_credits(film, "seekWhileSaved")
    assert (out["back"]["card"], out["back"]["asks"]) == (False, 0)
    assert (out["after"]["card"], out["after"]["asks"]) == (True, 1)


def test_a_film_that_ends_while_the_server_is_asked_is_asked_once(film):
    out = in_credits(film, "endWhileAsked")
    assert (out["in"]["card"], out["end"]["card"], out["end"]["asks"]) == (False, True, 1)


def test_the_arrows_move_between_the_thumbs_and_leave_the_film_alone(film):
    out = in_credits(film, "arrowOnThumb")
    assert (out["left"], out["right"], out["moved"]) == (True, True, 0)
    assert out["after"]["card"] is True


def test_a_rating_in_the_credits_closes_the_card_and_they_go_on(film):
    out = in_credits(film, "rated")
    assert (out["rated"]["card"], out["rated"]["paused"]) == (False, False)
    assert out["rated"]["at"] == "/watch/play/1"
    # Nothing is left to wait for at the end.
    assert (out["end"]["asks"], out["end"]["at"]) == (1, "/watch/movie/501")


@pytest.mark.parametrize("what", ["close", "escape"])
def test_closing_the_question_in_the_credits_neither_stops_nor_leaves(film, what):
    out = in_credits(film, what)
    assert (out["after"]["card"], out["after"]["paused"]) == (False, False)
    assert out["after"]["at"] == "/watch/play/1"
    assert (out["end"]["card"], out["end"]["asks"]) == (False, 1)


def test_the_level_given_is_there_to_be_named_until_it_is_taken_back(film):
    out = in_credits(film, "levels")
    assert (out["name"], out["none"], out["rated"]) == ("I love it", "", False)


def test_the_question_steps_over_a_skip_button_that_it_meets(film):
    out = in_credits(film, "meets")
    assert (out["above"], out["beside"]) == (True, False)


def test_space_after_a_click_on_a_thumb_is_the_film_s(film):
    out = in_credits(film, "spaceAfterClick")
    assert (out["level"], out["after"]["paused"]) == (0, True)


@pytest.mark.parametrize("what", ["asked", "stillOpen"])
def test_the_question_still_open_at_the_end_waits_as_ever_and_leads_on(film, what):
    out = in_credits(film, what)
    assert (out["end"]["card"], out["end"]["asks"], out["end"]["at"]) == (True, 1, "/watch/play/1")
    assert (out["nine"]["card"], out["late"]["at"]) == (True, "/watch/movie/501")


def test_a_question_put_at_the_end_is_not_taken_back_into_the_credits(film):
    out = in_credits(film, "endCard")
    assert (out["end"]["card"], out["sought"]["card"]) == (True, False)
    assert out["late"]["at"] == "/watch/play/1"


@pytest.mark.parametrize("what", ["scene", "sceneSkip"])
def test_a_scene_after_the_credits_is_left_alone_and_the_question_waits_for_the_end(film, what):
    out = in_credits(film, what)
    assert out["in"]["card"] is True
    assert (out["scene"]["card"], out["scene"]["paused"]) == (False, False)
    assert (out["end"]["card"], out["end"]["asks"], out["end"]["at"]) == (True, 1, "/watch/play/1")


def test_left_alone_in_the_credits_the_question_goes_and_comes_at_the_end(film):
    out = in_credits(film, "idle")
    assert (out["nearly"]["card"], out["gone"]["card"]) == (True, False)
    # Only at the end is it the last time.
    assert out["nearly"]["idle"] == "Only you see this."
    assert out["end"]["idle"] == "Only you see this. You won’t be asked again."
    assert (out["after"]["card"], out["after"]["paused"]) == (False, False)
    assert (out["end"]["card"], out["end"]["asks"], out["end"]["at"]) == (True, 1, "/watch/play/1")


@pytest.mark.parametrize("what", ["idleFocus", "idleHover", "idleHidden", "idleTouch"])
def test_the_question_stays_while_somebody_is_at_it_or_nobody_can_see_it(film, what):
    out = in_credits(film, what)
    assert out["nearly"]["card"] is True
    assert out["after"]["card"] is (what != "idleHover")


def test_a_pause_is_no_time_that_the_question_was_left_alone_for(film):
    out = in_credits(film, "idlePaused")
    assert (out["paused"]["card"], out["after"]["card"]) == (True, False)


def test_a_rating_given_before_the_scene_is_not_asked_for_again(film):
    out = in_credits(film, "sceneRated")
    assert out["scene"]["card"] is False
    assert (out["end"]["card"], out["end"]["at"]) == (False, "/watch/movie/501")


def test_the_credits_ask_nothing_in_a_hidden_page_and_at_once_on_return(film):
    out = in_credits(film, "hidden")
    assert (out["hidden"]["card"], out["hidden"]["asks"]) == (False, 0)
    assert (out["after"]["card"], out["after"]["asks"]) == (True, 1)
    # Gone back before the credits, the way into them is to be played again.
    assert in_credits(film, "hiddenBack")["after"]["asks"] == 0


def test_a_seek_from_the_end_back_into_the_credits_calls_the_way_out_off(film):
    out = in_credits(film, "endSeek")
    assert (out["sought"]["card"], out["soughtLate"]["at"]) == (True, "/watch/play/1")


@pytest.mark.parametrize(("what", "asks"), [("askFails", 2), ("saveFails", 1), ("refused", 2)])
def test_what_the_credits_could_not_ask_is_asked_once_more_at_the_end(film, what, asks):
    out = in_credits(film, what)
    assert out["in"]["card"] is False
    assert (out["end"]["card"], out["end"]["asks"]) == (True, asks)


def test_the_question_open_at_a_season_s_end_leaves_the_countdown_as_it_is(markup):
    out = in_credits(markup, "asked")
    assert (out["end"]["card"], out["end"]["upnext"], out["end"]["asks"]) == (True, True, 1)
    assert (out["nine"]["card"], out["nine"]["upnext"]) == (True, True)
    assert (out["late"]["card"], out["went"]) == (False, True)


def test_the_question_open_with_autoplay_off_stays_beside_the_next_episode(markup):
    out = run(markup, "credits", saved={}, plays="noAutoplay")
    assert (out["late"]["card"], out["late"]["upnext"], out["went"]) == (True, True, False)


def test_the_question_open_gives_way_to_are_you_still_watching(markup):
    out = in_credits(markup, "stillWatching")["end"]
    assert (out["dialog"], out["card"]) == (True, False)


# Playing elsewhere: the Play on sheet, TVs with a page of their own, AirPlay, and a way such as
# Google Cast.

ELSEWHERE = {"mediaman.watch.autoplay": "true"}


def elsewhere(markup: Path, scenario: str, mode: str = "tvs", **kwargs: Any) -> Any:
    return run(markup, scenario, saved=ELSEWHERE, plays=mode, **kwargs)


@pytest.mark.parametrize(
    ("mode", "rows"),
    [
        ("plain", ["close", "here", "tv-5", "tv-7", "tv-6", "pair"]),
        ("tvs", ["close", "here", "tv-5", "tv-7", "airplay", "tv-6", "pair"]),
        # mediaman has no TVs to look for, or not for this person.
        ("airOnly", ["close", "here", "airplay"]),
        ("offline", ["close", "here", "airplay", "again", "pair"]),
    ],
)
def test_the_sheet_lists_what_the_title_can_play_on(markup, mode, rows):
    out = elsewhere(markup, "away_sheet", mode)
    assert (out["shown"], out["rows"], out["left"]) == (True, rows, True)


def test_with_nothing_to_play_on_there_is_no_button(markup):
    assert elsewhere(markup, "away_sheet", "absent") == {"shown": False}


def test_a_tv_is_given_the_place_and_nothing_is_started_here_while_it_plays(markup):
    out = elsewhere(markup, "away_tv")
    assert out["stopped"] == [{"position_s": 30, "played_s": 0}]
    assert out["play"] == {"url": "/api/tvs/5/play", "key": "501", "at": 30, "quality": "auto"}
    assert out["sheet"] is False
    assert out["state"]["says"] == "Playing on Living room"
    # Keys, controls, a connection that comes back, the page cache and time: none starts it here.
    assert (out["starts"], out["startsThere"], out["state"]["message"]) == (1, 1, None)


def test_brought_here_it_goes_on_at_the_tvs_place(markup):
    out = elsewhere(markup, "away_tv")
    assert (out["startsHere"], out["from"], out["after"]["away"]) == (2, 77, False)


@pytest.mark.parametrize(
    ("why", "acts"),
    [
        ("stopped", None),
        ("moved", ["Close", "Bring it here"]),
        ("lost", ["Try again", "Bring it here", "Close"]),
        ("unpaired", ["Bring it here", "Close"]),
    ],
)
def test_a_tv_that_is_over_leaves_the_place_here_and_waits_for_a_press(markup, why, acts):
    out = elsewhere(markup, "away_tv_over", why)
    assert (out["started"], out["time"], out["from"]) == (0, "1:28", 88)
    assert (out["message"] or {}).get("acts") == acts
    # Behind its card the page goes on saying where it was.
    assert out["away"] is (acts is not None)
    if acts:
        assert (out["says"], out["hint"]) == (
            "Was playing on Living room",
            "Your place is saved at 1:28.",
        )


@pytest.mark.parametrize("how", ["again", "limit", "closed"])
def test_a_tv_that_does_not_start_says_so_and_it_plays_on_here(markup, how):
    out = elsewhere(markup, "away_tv_refused", how)
    assert (out["state"]["away"], out["from"]) == (False, 30)
    says = out["toasts"][0] if how == "closed" else out["says"]
    assert ("three screens" if how == "limit" else "Living room didn’t start") in says


def test_try_again_asks_the_tv_again(markup):
    out = elsewhere(markup, "away_tv_refused", "again")
    assert (out["plays"], out["second"]) == (2, "Starting on Living room")


def test_a_tv_that_is_not_open_says_how_to_open_it_and_touches_nothing(markup):
    out = elsewhere(markup, "away_tv_asks")
    assert "mm/tv" in out["off"]
    assert out["untouched"] is True


def test_a_titles_own_button_plays_it_on_a_tv_at_its_saved_place(markup):
    saved = {"mediaman.watch.quality": "720p-4000", "mediaman.watch.autoplay": "false"}
    out = run(markup, "away_title", saved=saved, plays="tvs")
    assert out["rows"] == ["close", "here", "tv-5", "tv-7", "airplay", "tv-6", "pair"]
    assert out["play"] == [
        {"rating_key": "501", "quality": "medium", "autoplay": False, "asked_s": 0}
    ]
    assert out["opened"] == 5
    assert (out["on"], out["went"]) == ("true", "/watch/play/501?resume=1")


def test_a_titles_own_button_is_not_there_with_nothing_to_play_on(markup):
    assert elsewhere(markup, "away_title", "absent") == {"hidden": True}


def test_a_tv_is_paired_in_the_sheet_named_and_played_on(markup):
    out = elsewhere(markup, "away_pair")
    assert out["focus"] is True
    assert "mm/tv" in out["steps"]
    # Sent by the sixth number, not before.
    assert out["early"] == 0
    assert "isn’t right" in out["wrong"][0]
    assert out["wrong"][1] == ""
    assert "stops what they’re watching" in out["others"][0]
    assert out["sent"] == [["123456", False], ["482916", False], ["482916", True]]
    assert out["name"] == "Living room"
    assert out["named"] == [["/api/tvs/9", "Den"]]
    assert out["plays"] == ["/api/tvs/9/play"]


def test_a_tv_somebody_watches_on_is_taken_only_after_the_question(markup):
    out = elsewhere(markup, "away_tv_asks")
    assert "Someone is watching" in out["busy"]
    assert (out["cancelled"], out["before"], out["after"]) == (None, [], [True])


def test_a_tv_found_in_use_asks_first_and_is_then_taken(markup):
    out = elsewhere(markup, "away_tv_in_use")
    assert "Someone is watching" in out["asks"]
    assert (out["from"], out["plays"]) == (30, [False, True])


def test_called_back_before_the_tv_began_it_plays_here_and_the_tv_is_stopped(markup):
    out = elsewhere(markup, "away_tv_called_back")
    assert out == {"here": 1, "state": False, "stopped": True, "after": False}


def test_airplay_changes_nothing_until_a_tv_is_picked(markup):
    out = elsewhere(markup, "away_airplay")
    assert out["picked"] == 1
    assert out["waits"] == {
        "casts": 0,
        "destroyed": False,
        "away": False,
        "clip": "/static/video/airplay.mp4",
    }


def test_airplay_stops_the_stream_here_before_it_starts_the_tvs_at_the_same_place(markup):
    out = elsewhere(markup, "away_airplay")
    assert out["order"] == [34, "501"]
    assert (out["src"], out["at"]) == ("http://mm/api/cast/T1/index.m3u8", 34)
    assert (out["playing"]["says"], out["local"]) == ("Playing on AirPlay", 1)
    assert out["saved"]["stream_id"] == "c1"
    assert out["saved"]["position_s"] > 40


def test_as_airplay_ends_it_plays_on_here_from_the_same_place(markup):
    out = elsewhere(markup, "away_airplay")
    assert out["back"] == {
        "away": False,
        "from": 46,
        "starts": 2,
        "clip": "/static/video/airplay.mp4",
    }
    assert out["stopped"] == "/api/watch/stream/c1/stop 46"
    # Safari pauses what it takes back; that is not the person's pause.
    assert out["playsOn"] == 2


@pytest.mark.parametrize(("how", "says"), [("late", None), ("tv", "Starting on Living room")])
def test_airplay_that_is_no_longer_asked_for_does_not_start(markup, how, says):
    out = elsewhere(markup, "away_airplay_dropped", how)
    assert out == {"casts": 0, "says": says, "started": 1}


def test_a_stream_refused_to_airplay_is_said_and_it_plays_on_here(markup):
    out = elsewhere(markup, "away_airplay_refused")
    assert "three screens" in out["says"]
    assert (out["state"], out["from"], out["clip"]) == (False, 30, "/static/video/airplay.mp4")


def test_a_choice_for_the_tv_restarts_it_there_and_is_not_kept_for_here(markup):
    out = elsewhere(markup, "away_choices")
    assert out["qualities"] == ["auto", "best", "medium", "low"]
    assert out["quality"] == [[200, False]]
    assert out["asked"] == [["auto", None], ["low", None], ["low", "off"]]
    assert (out["saved"], out["local"]) == (None, 1)
    assert out["here"]["start"] == "auto"
    assert "original" in out["here"]["qualities"]
    assert (out["here"]["tracks"], out["here"]["stops"]) == (True, 1)


def test_subtitles_a_tv_cannot_have_give_way_to_those_it_had(markup):
    assert elsewhere(markup, "away_choices")["refused"] == ["32", "off"]


def test_the_next_episode_goes_on_where_this_one_played(markup, tmp_path_factory):
    next_item = {**EPISODE["item"], "rating_key": "502", "subtitle": "S2 · E5"}
    next_page = _markup(tmp_path_factory, {**EPISODE, "item": next_item})
    out = elsewhere(markup, "away_next", pages={"/watch/play/502": next_page})
    assert out["casts"] == ["501", "502"]
    assert (out["loads"], out["local"], out["away"]) == (["502"], 1, True)
    assert out["qualities"] == ["auto", "best", "medium", "low"]


@pytest.mark.parametrize("last", [False, True])
def test_the_credits_and_the_end_are_the_players_own_on_a_tv_too(markup, last_episode, last):
    out = elsewhere(last_episode if last else markup, "away_end")
    assert out["rate"] is True
    assert (out["saved"], out["upnext"]) == (["c1", 2997], not last)
    # The TV may be given something else from here.
    assert (out["says"], out["leaves"]) == ("Finished on AirPlay", "/watch/play/1")


def test_leaving_the_page_asks_first_where_that_ends_it(markup):
    out = elsewhere(markup, "away_leave", "air")
    assert (out["asked"], out["kept"]) == (True, True)
    assert out["beacons"] == [["/watch/stream/c1/stop", 62]]
    out = elsewhere(markup, "away_leave_and_stop", "air")
    assert out["beacons"] == [["/api/watch/stream/c1/stop", 60]]
    assert (out["went"], out["clip"]) == ("/watch/show/77", "/static/video/airplay.mp4")


def test_leaving_the_page_leaves_a_tv_that_carries_on_alone(markup):
    out = elsewhere(markup, "away_leave", "cast")
    assert out["asked"] is False
    assert (out["beacons"], out["stops"]) == ([["/watch/progress", 62]], 0)
    # Back from the page cache it is still what the page follows.
    assert (out["started"], out["casts"]) == (0, 1)


def test_a_stream_that_ends_under_the_tv_is_opened_anew_twice_at_most(markup):
    out = elsewhere(markup, "away_error")
    assert (out["loads"], out["casts"], out["local"]) == ([90, 90], 3, 1)
    assert out["message"]["button"] == "Try again"


@pytest.mark.parametrize("why", ["lost", "taken", "stopped"])
def test_what_ended_elsewhere_by_itself_waits_here_for_a_press(markup, why):
    out = elsewhere(markup, "away_gone", why)
    assert (out["says"], out["starts"], out["stopped"]) == ("Was playing on Den", 1, 120)
    assert (out["message"]["acts"], out["from"]) == (["Bring it here", "Close"], 120)


def test_a_title_started_on_another_page_ends_here_and_on_the_tv(markup):
    out = elsewhere(markup, "away_replaced")
    assert (out["away"], out["stops"], out["starts"]) == (False, 1, 2)
    assert out["message"]["button"] == "Play here"


def test_moved_from_one_tv_to_another_it_keeps_its_place(markup):
    out = elsewhere(markup, "away_moves")
    assert out == {"stops": 1, "at": 150, "says": "Starting on Living room", "starts": 1}


def test_a_play_that_outlived_its_page_is_followed_and_not_started_anew(markup):
    out = elsewhere(markup, "away_again", "same")
    assert (out["starts"], out["left"], out["says"]) == (0, 0, "Playing on Den")
    assert out["saved"] == {"stream_id": "k1", "position_s": 302, "played_s": 121}


def test_a_play_of_another_title_is_left_to_its_tv(markup):
    assert elsewhere(markup, "away_again", "other") == {"starts": 1, "left": 1, "says": None}


def test_a_paused_tv_is_kept_alive_by_its_last_save_and_nothing_after_it_is_back(markup):
    out = elsewhere(markup, "away_paused")
    assert out["first"] == [["c1", 50, 0]]
    assert (out["more"], out["same"], out["here"]) == (30, True, 0)


@pytest.mark.parametrize("how", ["back", "lost", "taken", "here", "stop"])
def test_what_comes_back_during_the_countdown_does_not_go_on_by_itself(markup, how):
    out = elsewhere(markup, "away_countdown", how)
    assert out == {"counts": True, "next": False, "card": False}


@pytest.mark.parametrize(("how", "told"), [("leave", [10]), ("cancel", [10, None])])
def test_a_page_left_at_the_end_has_its_way_count_on_as_it_did(markup, how, told):
    out = elsewhere(markup, "away_count_left", how)
    assert out == {"counts": told, "beacons": ["/watch/stream/c1/stop"]}


@pytest.mark.parametrize(
    ("how", "told"),
    [
        ("ended", ["501"]),
        ("skipped", ["501"]),
        # Left in the final credits, as the server counts it watched.
        ("creditsLeft", ["501"]),
        ("earlyLeft", []),
        ("left", []),
        ("there", []),
    ],
)
def test_the_ways_are_told_what_was_watched_here_to_its_end(markup, how, told):
    assert elsewhere(markup, "away_watched", how) == told


def test_the_keyboard_is_on_a_control_that_shows_as_what_follows_an_end_is_tried_again(markup):
    out = elsewhere(markup, "away_next_tried")
    assert (out["message"]["button"], out["ended"]) == ("Try again", True)
    assert out["hidden"] is False


def test_a_page_opened_while_a_tv_plays_its_title_leaves_it_there(markup):
    out = elsewhere(markup, "away_opened_on_tv", "same")
    assert (out["atOnce"], out["starts"], out["says"]) == (0, 0, "Paused on Living room")
    assert (out["from"], out["known"]) == (77, "1")


def test_a_page_back_from_the_cache_leaves_its_title_on_the_tv(markup):
    out = elsewhere(markup, "away_opened_on_tv", "cache")
    assert out == {"starts": 0, "says": "Paused on Living room"}


@pytest.mark.parametrize("how", ["other", "ended"])
def test_a_page_opened_while_a_tv_plays_something_else_plays_here(markup, how):
    out = elsewhere(markup, "away_opened_on_tv", how)
    assert (out["starts"], out["says"]) == (1, None)


def test_where_no_tv_was_known_to_play_the_title_starts_at_once(markup):
    out = elsewhere(markup, "away_opened_on_tv", "unknown")
    assert (out["atOnce"], out["after"]["starts"], out["message"]) == (1, 1, None)


@pytest.mark.parametrize(
    ("how", "after"),
    [
        ("silent", {"starts": 0, "says": None, "button": "Play here"}),
        ("lateNone", {"starts": 1, "says": None, "button": None}),
        ("lateTv", {"starts": 0, "says": "Paused on Living room", "button": None}),
        ("lateFails", {"starts": 0, "says": None, "button": "Play here"}),
    ],
)
def test_where_a_tv_was_known_to_play_the_wait_for_its_word_is_short(markup, how, after):
    out = elsewhere(markup, "away_opened_on_tv", how)
    assert (out["atOnce"], out["message"]) == (0, None)
    assert out["slow"] == {"starts": 0, "button": "Play here"}
    assert out["after"] == after
    # What was heard is known to the pages to come.
    assert out["known"] == (None if how == "lateNone" else "1")


def test_paused_while_it_is_not_known_where_it_plays_it_begins_paused(markup):
    out = elsewhere(markup, "away_opened_on_tv", "pausedLook")
    assert out == {"starts": 1, "plays": 0}


def test_a_press_while_it_is_not_known_where_it_plays_starts_nothing(markup):
    out = elsewhere(markup, "away_opened_on_tv", "pressed")
    assert (out["pressed"], out["starts"]) == (0, 0)
    assert out["says"] == "Paused on Living room"


def test_a_tv_that_begins_as_the_page_is_left_is_left_to_begin(markup):
    out = elsewhere(markup, "away_left_starting", "said")
    assert (out["aborted"], out["stops"], out["starts"]) == (False, 0, 0)
    assert out["says"] == "Starting on Living room"


def test_what_began_here_before_the_tv_said_that_it_begins_gives_way_to_it(markup):
    out = elsewhere(markup, "away_left_starting", "unsaid")
    # The stream the page was left with, and the one it came back with.
    assert (out["starts"], out["ended"], out["says"]) == (1, 2, "Starting on Living room")


def test_back_from_the_cache_a_way_is_asked_again(markup):
    assert elsewhere(markup, "away_again_cache") == {"starts": 0, "says": "Playing on Den"}


def test_a_tv_refused_to_a_title_that_may_be_on_another_starts_nothing_here(markup):
    out = elsewhere(markup, "away_again_refused", "lost")
    assert (out["asked"], out["starts"], out["pressed"]) == (True, 0, 1)
    assert out["message"]["button"] == "Play here"


@pytest.mark.parametrize("on", ["tv-5", "cast"])
def test_a_title_of_another_tab_is_said_to_be_there_still_after_a_tv_refused_it(markup, on):
    out = elsewhere(markup, "away_again_refused", f"elsewhere {on}")
    assert "another tab" in out["message"]["text"]


def test_a_page_told_that_a_tv_plays_its_title_leaves_it_there(tmp_path_factory):
    told = _markup(tmp_path_factory, {**EPISODE, "on_tv": True})
    out = elsewhere(told, "away_opened_on_tv", "told")
    assert (out["atOnce"], out["starts"], out["says"]) == (0, 0, "Paused on Living room")


def test_a_play_found_after_the_next_episode_was_asked_for_is_left(markup, tmp_path_factory):
    next_item = {**EPISODE["item"], "rating_key": "502", "subtitle": "S2 · E5"}
    next_page = _markup(tmp_path_factory, {**EPISODE, "item": next_item})
    out = elsewhere(markup, "away_again_passed", pages={"/watch/play/502": next_page})
    assert out == {"left": 1, "starts": ["502"], "says": None}


@pytest.mark.parametrize("how", ["found", "pressed", "overLate", "lost", "left"])
def test_a_title_that_may_be_on_a_tv_is_never_started_here_by_itself(markup, how):
    out = elsewhere(markup, "away_again_slow", how)
    assert (out["asked"], out["early"], out["starts"]) == ("501", 0, 0)
    assert out["message"]["button"] == "Play here"
    after = out["after"]
    if how == "found":
        assert (after["starts"], after["says"], after["message"]) == (0, "Playing on Den", None)
    elif how == "pressed":
        assert (after["starts"], after["left"], after["says"]) == (1, 1, None)
        # The way forgets the title before the start, so that it starts by itself next time.
        assert after["released"] == ["501", 0]
    else:
        assert (after["starts"], after["message"]["button"]) == (0, "Play here")


def test_back_to_a_title_that_may_be_on_a_tv_the_way_is_asked_again_and_nothing_started(markup):
    out = elsewhere(markup, "away_again_slow", "left")
    # It had not answered yet.
    assert out["times"] == 1
    out = elsewhere(markup, "away_again_slow", "leftLost")
    assert (out["again"]["times"], out["again"]["starts"]) == (2, 0)
    assert out["again"]["message"]["button"] == "Play here"
    assert (out["over"]["starts"], out["over"]["message"]["button"]) == (0, "Play here")
    assert (out["after"]["starts"], out["after"]["says"]) == (0, "Playing on Den")


@pytest.mark.parametrize(
    ("how", "place"),
    [
        # What plays it here said its place a moment ago.
        ("way", 900),
        # It said it long ago, or never: what the server has now.
        ("old", 640),
        ("none", 640),
        # The person chose another one meanwhile.
        ("sought", 10),
    ],
)
def test_played_here_after_all_it_goes_on_from_its_newest_place(markup, how, place):
    out = elsewhere(markup, "away_again_place", how)
    assert (out["starts"], out["from"], out["message"]) == (1, place, None)


def test_played_here_after_all_it_does_not_start_from_a_place_it_cannot_know(markup):
    out = elsewhere(markup, "away_again_place", "offline")
    assert (out["starts"], out["message"]["button"], out["again"]) == (0, "Play here", 640)


def test_what_ended_on_a_tv_goes_on_from_where_it_was_taken_since(markup):
    assert elsewhere(markup, "away_gone_place") == 800


@pytest.mark.parametrize("how", ["fails", "failsFirst"])
def test_a_play_a_way_finds_is_followed_where_the_tvs_cannot_be_looked_at(markup, how):
    out = elsewhere(markup, "away_again_and_tv", how)
    assert (out["left"], out["starts"], out["says"]) == (0, 0, "Playing on Den")
    assert out["after"] is None


def test_where_neither_is_known_nothing_starts_by_itself(markup):
    out = elsewhere(markup, "away_again_and_tv", "failsNull")
    assert (out["starts"], out["after"]["button"]) == (0, "Play here")


@pytest.mark.parametrize("how", ["tv", "tvFirst"])
def test_a_play_a_way_finds_is_left_where_a_tv_has_the_title(markup, how):
    out = elsewhere(markup, "away_again_and_tv", how)
    assert (out["left"], out["starts"], out["says"]) == (1, 0, "Playing on Living room")


def test_a_play_found_before_the_next_episode_was_asked_for_is_left(markup):
    out = elsewhere(markup, "away_again_and_tv", "nextFirst")
    assert (out["left"], out["starts"]) == (1, 0)


@pytest.mark.parametrize("how", ["pressed", "pressedFirst"])
def test_paused_while_it_is_looked_for_it_is_paused_where_it_plays(markup, how):
    out = elsewhere(markup, "away_again_and_tv", how)
    assert (out["says"], out["paused"]) == ("Paused on Den", True)


def test_a_title_that_another_tab_plays_on_a_tv_says_so_at_once(markup):
    out = elsewhere(markup, "away_again_slow", "elsewhere")
    assert (out["told"]["button"], out["early"], out["starts"]) == ("Play here", 0, 0)
    assert "another tab" in out["told"]["text"]
    assert (out["after"]["starts"], out["after"]["released"]) == (1, ["501", 0])


def test_a_title_known_not_to_be_on_a_tv_plays_here_at_once(markup):
    out = elsewhere(markup, "away_again_slow", "over")
    assert (out["early"], out["message"]) == (1, None)


def test_a_rejoined_play_has_its_subtitles_chapters_and_marks_and_is_kept_alive(markup):
    out = elsewhere(markup, "away_kept")
    assert out["tracks"] == ["off", "31*", "32"]
    assert (out["chapters"], out["skip"]) == (5, "Next episode")
    # And a second reload has them too.
    assert out["keptAgain"] == [3, 5]
    # What follows is there for the pages without a player.
    assert out["next"] == {"rating_key": "502", "label": "S2 · E5 — High Tide"}
    # No time played is told of it, so that it is no save.
    assert out["saves"] == [["k1", 2710, 0]] * 30


@pytest.mark.parametrize("how", ["tv", "tvStop", "way", "wayStop"])
def test_called_back_while_it_begins_elsewhere_the_sheet_is_free_again(markup, how):
    out = elsewhere(markup, "away_cancelled", how)
    assert out["says"].startswith("Starting on")
    starts = 0 if "Stop" in how else 1
    assert out["here"] == {"starts": starts, "away": False, "row": "true"}
    # What a TV was asked for cannot be called off: it is stopped once it has begun, and only it.
    assert out["aborted"] is how.startswith("way")
    assert out["early"] == []
    assert out["stopped"] == (["p7"] if how.startswith("tv") else [])
    assert (out["after"], out["busy"], out["again"]) == (False, None, "Starting on Living room")


def test_a_stream_for_a_way_that_is_never_answered_plays_on_here(markup):
    out = elsewhere(markup, "away_cancelled", "wayLate")
    assert "Chromecast didn’t start" in out["told"]
    assert (out["aborted"], out["here"]["starts"]) == (True, 1)


def test_a_play_that_ended_for_one_that_did_not_start_is_not_called_unchanged(markup):
    out = elsewhere(markup, "away_moved_refused")
    assert (out["stops"], out["from"]) == (1, 150)
    assert "Nothing changed" not in out["says"]
    assert "Den" in out["says"]


def test_an_open_sheet_looks_on_after_a_look_that_failed(markup):
    out = elsewhere(markup, "away_looks_on", "plain")
    assert out == {"failed": "On and ready", "later": "Not open"}


CARD = "It may be off or away from the network. If it comes back within 2 minutes, it carries on by itself."


@pytest.mark.parametrize("how", ["found", "here", "stop"])
def test_a_way_whose_tv_does_not_answer_says_so_until_it_does(markup, how):
    out = elsewhere(markup, "away_lost_way", how)
    lost = out["lost"]
    assert lost["says"] == "Was playing on Den"
    assert lost["message"]["text"] == "Den stopped answering"
    assert (lost["message"]["line"], lost["message"]["acts"]) == (CARD, ["Bring it here", "Stop"])
    assert (out["inert"], out["inertAfter"], out["starts"]) == (True, False, 1)
    assert out["after"]["message"] is None
    if how == "found":
        assert (out["after"]["says"], out["stops"], out["startsAfter"]) == ("Playing on Den", 0, 1)
    elif how == "here":
        assert (out["after"]["away"], out["stops"], out["from"]) == (False, 1, 120)
    else:
        assert (out["after"]["away"], out["stops"], out["startsAfter"]) == (False, 1, 1)


@pytest.mark.parametrize("how", ["again", "back"])
def test_a_tv_that_does_not_answer_has_the_remotes_card(markup, how):
    out = elsewhere(markup, "away_lost_tv", how)
    lost = out["lost"]
    assert (lost["says"], lost["hint"]) == (
        "Was playing on Living room",
        "Your place is saved at 0:30.",
    )
    assert lost["message"]["text"] == "Living room stopped answering"
    assert "away from its browser" in lost["message"]["line"]
    assert lost["message"]["acts"] == ["Try again", "Bring it here", "Stop"]
    if how == "again":
        assert out["asked"] == 1
    else:
        assert (out["after"]["says"], out["after"]["message"]) == ("Playing on Living room", None)


def test_an_older_tv_that_does_not_answer_is_not_said_to_have_a_browser(markup):
    line = elsewhere(markup, "away_lost_tv", "older")["lost"]["message"]["line"]
    assert "browser" not in line


def test_a_tv_lost_for_good_can_be_asked_again_from_the_same_place(markup):
    out = elsewhere(markup, "away_lost_tv", "over")
    assert out["over"]["message"]["acts"] == ["Try again", "Bring it here", "Close"]
    assert "at 1:28" in out["over"]["message"]["line"]
    assert (out["plays"], out["starts"], out["startsAfter"]) == ([30, 88], 1, 1)
    assert (out["after"]["says"], out["after"]["message"]) == ("Starting on Living room", None)


def test_a_press_meant_for_the_picture_does_nothing_to_a_tv_that_does_not_answer(markup):
    out = elsewhere(markup, "away_lost_way", "keys")
    # No button of the card that came by itself has the keyboard, nor any under it.
    assert (out["focus"], out["endInert"]) == (True, [True, True])
    assert out["keys"] == {"stops": 0, "moved": 0, "pages": 0}


def test_the_keyboard_is_on_the_controls_again_as_a_tv_answers(markup):
    assert elsewhere(markup, "away_lost_way", "found")["back"] == [True, False]
    assert elsewhere(markup, "away_lost_tv", "back")["focus"] is True


@pytest.mark.parametrize(
    ("how", "hint"),
    [
        ("cancel", "The next episode starts by itself, even if you leave this page."),
        ("cancelStays", "Keep this page open for the next episode."),
    ],
)
def test_the_page_says_what_the_next_episode_waits_for(markup, how, hint):
    out = elsewhere(markup, "away_countdown", how)
    assert out["hint"] == hint
    assert out["after"] == "Den is ready for something else."
    assert out["focus"] is True


def test_a_tv_that_is_still_off_keeps_the_card(markup):
    out = elsewhere(markup, "away_lost_tv", "overRefused")
    assert out["after"]["message"]["acts"] == ["Try again", "Bring it here", "Close"]
    assert "answering" in out["toasts"][-1]
    assert out["startsAfter"] == 1


def test_a_tv_taken_by_somebody_is_said_and_can_be_closed(markup):
    out = elsewhere(markup, "away_lost_tv", "overTaken")
    assert out["over"]["message"]["text"] == "Someone else is using Living room"
    assert out["over"]["message"]["acts"] == ["Bring it here", "Close"]
    assert (out["after"]["away"], out["after"]["message"], out["startsAfter"]) == (False, None, 1)


BAD_FILE = "Its picture is in a form older TVs refuse, and converting it failed. Try another TV, or watch here."


def test_an_older_tv_that_cannot_play_the_film_says_so_in_the_sheet(markup):
    out = elsewhere(markup, "away_older", "tv")
    assert (out["waits"], out["open"]) == ("true", True)
    assert out["says"] == "Kitchen TV can’t play this one" + BAD_FILE
    assert (out["state"]["away"], out["stops"]) == (False, ["p7"])


def test_an_older_tv_that_fails_otherwise_is_not_said_to_refuse_the_film(markup):
    says = elsewhere(markup, "away_older", "unreachable")["says"]
    assert "older TVs refuse" not in says


def test_the_sheet_closes_as_an_older_tv_has_begun(markup):
    out = elsewhere(markup, "away_older", "plays")
    assert (out["waits"], out["open"], out["says"]) == ("true", False, None)
    assert (out["state"]["says"], out["stops"]) == ("Playing on Kitchen TV", [])


def test_the_page_says_that_a_tv_could_not_play_it(markup):
    older = elsewhere(markup, "away_failed_tv", "older")
    assert (older["says"], older["hint"]) == ("Couldn’t play on Living room", BAD_FILE)
    assert elsewhere(markup, "away_failed_tv", "page")["hint"] == "Your place is kept."


def test_a_tv_just_paired_is_asked_until_it_is_heard_of(markup):
    out = elsewhere(markup, "away_fresh", "soon")
    assert (out["waits"], out["plays"]) == (None, 3)
    assert out["state"].endswith("on Living room")
    out = elsewhere(markup, "away_fresh", "never")
    # Not for ever.
    assert 3 < out["plays"] < 10
    assert "didn’t start" in out["says"]


def test_full_screen_is_left_as_it_begins_elsewhere(markup):
    assert elsewhere(markup, "away_fullscreen") == 1


def test_only_what_the_tv_was_known_to_play_is_stopped(markup):
    out = elsewhere(markup, "away_stop_id")
    assert (out["ids"], out["starts"]) == (["p1"], 2)
    out = elsewhere(markup, "away_stop_id", "stale")
    assert (out["ids"], out["starts"]) == (["p1"], 1)
    assert "Something else" in out["toasts"][-1]
    assert out["hint"] == "It plays Another now."


def test_a_paused_play_that_is_found_says_where_it_stands(markup):
    out = elsewhere(markup, "away_paused_found")
    assert (out["time"], out["dim"]) == ("30:00", True)
    assert out["names"] == ["Unmute the TV", "TV volume"]
    assert (out["here"], out["dimHere"]) == (["Mute", "Volume"], False)


def test_the_page_under_a_remote_is_dimmed_only_while_the_tv_is_paused(markup):
    out = elsewhere(markup, "away_tv_look")
    assert out == {"playing": [], "paused": ["is-away-paused"]}


def test_what_ended_on_a_tv_is_marked_as_ended(markup, last_episode):
    out = elsewhere(last_episode, "away_end")
    # And not as paused.
    assert (out["ended"], out["dim"]) == (True, False)


def test_rows_stay_where_they_are_while_the_sheet_is_open(markup):
    out = elsewhere(markup, "away_rows", "plain")
    keys = [key for key, _ in out["first"]]
    assert keys == ["here", "tv-5", "tv-7", "cast", "tv-6", "pair"]
    # It is there before it is known, and so is a way that fails not.
    assert dict(out["first"])["cast"] == "Looking for TVs…"
    assert out["found"] == [["cast", ""]]
    assert out["held"].endswith("px")
    later = [key for key, _ in out["later"]]
    assert later == ["here", "tv-5", "tv-7", "cast", "tv-6", "tv-11", "pair"]
    assert dict(out["later"])["tv-5"] == "Not open"
    assert dict(out["later"])["cast"] == "No TV found"


LOOKS = ["here", "pair"]
ROWS = ["here", "tv-5", "tv-7", "cast", "tv-6", "pair"]


def test_the_first_rows_come_in_order_and_the_sheet_grows_for_them(markup):
    out = elsewhere(markup, "away_first", "plain")
    assert (out["first"], out["heard"]) == (LOOKS, ROWS)
    floor, height = out["style"]
    assert floor.endswith("px")
    assert not height


@pytest.mark.parametrize(("how", "left"), [("mouse", ROWS), ("finger", LOOKS)])
def test_no_row_comes_while_the_pointer_is_on_one(markup, how, left):
    out = elsewhere(markup, "away_first", how)
    assert out["heard"] == LOOKS
    # A finger that lifts has yet to press.
    assert (out["left"], out["later"]) == (left, ROWS)


def test_the_browsers_own_ways_do_not_wait_for_tvs_that_are_slow_to_be_heard_of(markup):
    out = elsewhere(markup, "away_first", "slow")
    assert (out["first"], out["slow"]) == (LOOKS, ["here", "cast", "pair"])
    assert out["heard"] == ["here", "cast", "tv-5", "tv-7", "tv-6", "pair"]


def test_a_finger_that_left_one_sheet_says_nothing_of_the_next(markup):
    assert elsewhere(markup, "away_first", "reopened") is False


def test_a_way_that_cannot_work_says_why_and_is_looked_for_again(markup):
    out = elsewhere(markup, "away_rows", "plain")
    assert out["trouble"] == "Chromecast isn’t answeringGoogle’s script didn’t load."
    # By a press the sheet may grow.
    assert (out["freed"], out["looks"]) == ("", 2)


@pytest.mark.parametrize(("how", "says"), [("asks", True), ("not", False)])
def test_the_browsers_list_is_announced_only_where_it_opens(markup, how, says):
    assert (elsewhere(markup, "away_asks", how) is not None) is says


def test_the_sheet_stays_in_its_place_where_its_button_goes(markup):
    out = elsewhere(markup, "away_place", "plain")
    assert out["before"][0] not in ("", "12px")
    assert out["after"] == out["before"]


def test_a_title_starts_a_cast_itself_at_its_saved_place(markup):
    out = elsewhere(markup, "away_title_ways", "plain")
    assert out["rows"] == ["close", "here*", "tv-5", "tv-7", "cast", "tv-6", "pair"]
    # The browser's list is asked for in the press; the play is left to its way.
    assert out["steps"] == ["start", "keep", "leave", "remote cast"]
    assert out["asked"] == {"key": "501", "quality": "auto", "asked_s": 0}
    assert out["stream"] == {
        "at": 640,
        "url": "http://mm/api/cast/T1/index.m3u8",
        "title": "The Long Coast",
    }
    assert out["kept"] == {"next": {"rating_key": "502", "label": "S2 · E5"}, "tracks": 2}


def test_a_cast_refused_to_a_title_is_said_and_leaves_no_stream(markup):
    out = elsewhere(markup, "away_title_ways", "refused")
    assert "three screens" in out["says"]
    out = elsewhere(markup, "away_title_ways", "noTake")
    assert (out["says"], out["ended"]) == ("The TV didn’t startChoose it again.Try again", 1)


def test_a_cast_called_back_before_the_tv_took_it_leaves_no_stream(markup):
    out = elsewhere(markup, "away_title_ways", "calledBack")
    assert (out["stopped"], out["ended"]) == (1, 1)
    assert "keep" not in out["steps"]


def test_a_title_that_no_tv_plays_any_more_starts_again_from_its_row(markup):
    out = elsewhere(markup, "away_title_ways", "waits")
    assert "cast" in out["rows"]
    assert "start" in out["steps"]


def test_airplay_of_a_title_leads_to_the_player_and_its_sheet(markup):
    out = elsewhere(markup, "away_title_ways", "airplay")
    assert "airplay" in out["rows"]
    assert out["went"] == "/watch/play/501?resume=1#play-on"
    out = elsewhere(markup, "away_led")
    assert out == {"open": True, "url": "/watch/play/501?resume=1"}


def test_a_title_that_is_cast_says_so_and_this_device_brings_it_back(markup):
    out = elsewhere(markup, "away_title_ways", "here")
    assert "cast*" in out["rows"]
    assert "here*" not in out["rows"]
    assert "Bring it back here" in out["hereSays"]
    assert (out["steps"], out["went"]) == (["castStop"], "/watch/play/501?resume=1")
    # Of another tab: the player asks.
    out = elsewhere(markup, "away_title_ways", "otherTab")
    assert (out["steps"], out["went"]) == ([], "/watch/play/501?resume=1")


def test_a_titles_button_says_what_it_is_and_whether_a_tv_plays_it(markup):
    out = elsewhere(markup, "away_title_ways", "plain")
    assert (out["key"], out["label"], out["isOn"]) == ("501", "Play on a TV", False)
    out = elsewhere(markup, "away_title_ways", "playing")
    assert (out["label"], out["isOn"]) == ("Playing on a TV", True)
