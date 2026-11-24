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


def test_a_connection_that_failed_starts_anew_with_the_place_saved(markup):
    out = run(markup, "cancelled")
    assert out["restarted"] == 2
    # A page on its way out sends nothing else that is sure to arrive.
    assert [b["body"]["position_s"] for b in out["stopped"]] == [110]


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
    assert run(markup, "idle") == {"afterMouse": True, "afterTab": False}


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
    assert in_place["start"] == {"rating_key": "502", "quality": "low"}


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
    assert "next episode" in out["text"]
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
    assert out["start"] == {"rating_key": "501", "quality": "low"}


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
        "twice": through,
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
