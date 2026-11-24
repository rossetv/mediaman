"""player.js in a fake DOM: quality switches, the next-episode card, keys and controls.

Each test renders the real player markup, runs player.js on it through
``player_harness.js`` under node, and checks what the person would see.
"""

from __future__ import annotations

import json
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
            jinja2.DictLoader({"base.html": "{% block content %}{% endblock %}"}),
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


@pytest.fixture(scope="module")
def markup(tmp_path_factory) -> Path:
    html = _ENV.get_template("watch_player.html").render(**EPISODE)
    path = tmp_path_factory.mktemp("player") / "player.html"
    path.write_text(html)
    return path


def run(
    markup: Path,
    scenario: str,
    *,
    reduced: bool = False,
    saved: dict[str, str] | None = None,
    plays: str | None = None,
) -> Any:
    args = ["node", str(HARNESS), str(markup), scenario] + (["reduced"] if reduced else [])
    args += [json.dumps(saved)] if saved or plays else []
    args += [plays] if plays else []
    done = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_quality_switch_holds_the_last_frame_until_the_new_stream_plays(markup):
    out = run(markup, "quality_switch")
    assert out["whileLoading"]["hold"] == {"shown": True, "drewVideo": True}
    assert out["afterManifest"]["hold"]["shown"] is True
    assert out["afterPlaying"]["hold"]["shown"] is False


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


def test_the_centre_spinner_is_hidden_while_a_frame_is_held_and_back_after(markup):
    # The stylesheet hides the spinner under .is-held; the play button's ring stays.
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


def test_a_picked_quality_never_changes_by_itself(markup):
    saved = {"mediaman.watch.quality": "1080p-8000"}
    assert run(markup, "fixed_stalls", saved=saved) == ["1080p-8000"]


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
