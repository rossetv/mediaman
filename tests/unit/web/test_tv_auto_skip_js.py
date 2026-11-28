"""Skip intros and credits on the TV: tv.js under node against a fake TV browser, the phone
remote (tv-remote.js) on its own template, both playing a title with an intro and credits, and
Profile's switch for it (profile-playback.js)."""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

import jinja2
import pytest

from tests.helpers.node_batch import run_node

JS = Path(__file__).parent / "js"
TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"


def _run(harness: str, name: str, *args: str, **kwargs) -> dict:
    result = run_node(JS / harness, "skip", name, *args, timeout=60, **kwargs)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _tv(name: str, *args: str) -> dict:
    return _run("tv_harness.js", name, *args, node_args=("--expose-internals",))


@cache
def _template() -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    return env.get_template("_tv_remote.html").render()


def _remote(name: str) -> dict:
    return _run("tv_remote_harness.js", name, input=_template())


def _profile(name: str) -> dict:
    return _run("profile_playback_harness.js", name)


# ── on the TV ──


def test_the_tv_page_has_a_place_for_the_note_outside_the_pages_it_draws() -> None:
    assert _tv("page") is True


def test_the_tv_plays_up_to_an_intro_untouched() -> None:
    assert _tv("beforeIntro") == {"sets": []}


def test_an_intro_plays_a_beat_then_counts_down_on_screen_before_the_jump() -> None:
    out = _tv("counts")
    assert out["landed"] == {"sets": 0, "note": False}
    counting = out["counting"]
    assert (counting["sets"], counting["note"]["shown"]) == (0, True)
    assert re.fullmatch(r"Skipping intro in [345] · Press to cancel", counting["note"]["text"])


def test_back_during_the_count_keeps_the_intro_playing() -> None:
    out = _tv("kept")
    assert (out["counting"], out["hidden"], out["sets"]) == (True, True, [])
    assert out["at"] == 75


def test_the_count_is_said_once_with_its_seconds_and_its_ticking_number_is_not_read_out() -> None:
    assert _tv("spoken") == {
        "said": ["Skipping intro in 5 seconds. Press to cancel."],
        "hidden": [True],
    }


def test_a_bar_under_the_note_drains_over_the_whole_count() -> None:
    out = _tv("drains")
    assert out["duration"] == "5s"
    assert -0.5 <= out["delay"] <= 0


def test_ok_during_the_count_keeps_the_intro_playing_and_outside_it_skips_nothing() -> None:
    out = _tv("okKeeps")
    assert (out["before"], out["counting"], out["hidden"], out["sets"]) == ([], True, True, [])
    assert out["at"] == 75


def test_an_intro_too_short_for_the_whole_count_counts_what_fits_or_none() -> None:
    out = _tv("shortIntro")
    assert (out["said"], out["jumped"]) == (["Skipping intro in 3 seconds. Press to cancel."], [68])
    assert (out["tiny"]["jumped"], out["tiny"]["counted"]) == ([63], 0)


def test_a_count_goes_with_a_seek_out_a_step_within_or_another_play() -> None:
    out = _tv("stale")
    assert (out["seekOut"], out["stepped"], out["played"]) == (False, False, False)
    # Back pressed as the count went is not taken as a Back for the next play's intro.
    assert 150 <= out["nextSkips"] <= 152


def test_an_intro_is_jumped_past_after_its_count_with_a_note_that_then_goes() -> None:
    out = _tv("intro")
    assert 150 <= out["at"] <= 152
    assert out["note"] == {"shown": True, "text": "Skipped intro"}
    assert out["later"] is False


def test_with_the_setting_off_or_no_markers_the_intro_plays() -> None:
    assert _tv("off") == {"at": 65}
    assert _tv("noMarks") == {"at": 65}


def test_an_intro_skipped_once_plays_when_the_person_goes_back_into_it() -> None:
    assert _tv("back") == {"at": 75}


def test_the_last_credits_play_on_under_the_next_episode_s_card_and_its_countdown() -> None:
    out = _tv("credits")
    assert "Next episode in" not in out["before"]
    assert re.search(
        r"Next episode in 10 .* Press to cancel .* E5 .* Windfall Watch credits Play now",
        out["says"],
    )
    # Watch credits has the focus: whoever wants the next episode need only wait.
    assert (out["held"], out["playing"], out["jumped"], out["note"], out["asked"]) == (
        "b0",
        True,
        [],
        False,
        [],
    )


def test_the_card_over_the_last_credits_ends_the_episode_where_it_is_and_goes_straight_on() -> None:
    out = _tv("creditsRunOut")
    # No jump to the end, so no stream to fill again before the next episode.
    assert (out["jumped"], out["paused"]) == ([], True)
    assert (out["ended"]["state"], out["ended"]["position_s"], out["ended"]["counted"]) == (
        "ended",
        2880,
        True,
    )
    # mediaman's own countdown is not shown a second time.
    assert (out["asked"], out["cardAgain"]) == (["auto"], False)


@pytest.mark.parametrize("key", ["ok", "back"])
def test_watch_credits_keeps_the_last_credits_playing_to_the_end(key: str) -> None:
    out = _tv("watchCredits", key)
    assert (out["gone"], out["again"], out["paused"], out["ended"], out["asked"]) == (
        True,
        False,
        False,
        False,
        0,
    )
    assert out["at"] == 2725


def test_where_nothing_follows_by_itself_the_card_over_the_credits_waits_for_play_now() -> None:
    out = _tv("creditsNoAutoplay")
    assert "Next episode in" not in out["says"] and "Watch credits Play now" in out["says"]
    assert (out["held"], out["at"], out["paused"], out["asked"]) == ("b1", 2720, False, ["next"])


def test_a_pause_takes_the_card_away_and_it_counts_afresh_as_the_credits_play_on() -> None:
    out = _tv("creditsPaused")
    assert (out["paused"], out["ended"]) == (False, False)
    assert "Next episode in 10 " in out["again"]


def test_the_last_credits_play_4_s_before_the_card_comes() -> None:
    assert 2703.5 <= _tv("lastHeld") <= 2705


def test_credits_a_scene_follows_are_jumped_past_with_a_note() -> None:
    out = _tv("midCredits")
    assert 1260 <= out["at"] <= 1262
    assert out["note"] == {"shown": True, "text": "Skipped credits"}


def test_a_play_that_resumes_inside_an_intro_still_skips_it() -> None:
    out = _tv("resumedInIntro")
    assert 150 <= out["at"] <= 154
    assert out["note"]["text"] == "Skipped intro"


def test_a_seek_the_person_asks_for_into_an_intro_plays_the_intro() -> None:
    assert 85 <= _tv("seekedIn")["at"] <= 90


def test_the_next_title_played_skips_an_intro_at_the_same_place_again() -> None:
    assert 150 <= _tv("nextPlay")["at"] <= 152


def test_a_seek_the_browser_is_slow_to_carry_out_is_not_skipped_from_where_it_was() -> None:
    assert 300 <= _tv("slowToMove")["at"] <= 310


def test_within_an_intro_s_last_second_nothing_is_skipped() -> None:
    assert _tv("lastSecond") == {"sets": [], "note": False}


def test_the_credits_of_a_film_or_a_last_episode_play() -> None:
    assert _tv("filmCredits") == {"at": 2705, "note": False}
    assert _tv("lastEpisode") == {"at": 2705}


def test_credits_that_run_to_the_end_count_as_the_last_ones() -> None:
    # The card comes over them only where a next episode follows; otherwise they play.
    assert _tv("creditsToTheEnd") == {"card": True, "note": False, "plays": 2705, "offCard": False}


def test_a_paused_tv_skips_nothing() -> None:
    assert _tv("paused") == {"at": 70}


# ── on the phone remote ──


def test_with_the_setting_off_the_remote_skips_the_intro_on_screen() -> None:
    out = _remote("intro")
    assert out["shown"] == "Skip intro"
    assert out["sent"] == [{"kind": "seek", "position_s": 150, "play_id": "p1"}]


def test_the_remote_offers_no_skip_near_an_intro_s_end_or_outside_one() -> None:
    assert _remote("introEnds") is None
    assert _remote("outside") is None


def test_the_remote_offers_no_skip_where_the_tv_skips_by_itself() -> None:
    assert _remote("autoSkips") is None


def test_the_last_credits_of_an_episode_lead_on_to_the_next_from_the_remote() -> None:
    out = _remote("lastCredits")
    assert out["shown"] == "Next episode"
    assert out["sent"] == [{"kind": "next", "play_id": "p1"}]


def test_credits_a_scene_may_follow_are_skipped_not_led_on_from_on_the_remote() -> None:
    assert _remote("midCredits") == "Skip credits"


def test_a_film_s_credits_are_skipped_to_its_end_from_the_remote() -> None:
    out = _remote("filmCredits")
    assert out["shown"] == "Skip credits"
    assert out["sent"] == [{"kind": "seek", "position_s": 2880, "play_id": "p1"}]


# ── in Profile ──


def test_the_switch_saves_each_way_and_says_so() -> None:
    out = _profile("off")
    assert out["off"]["state"] == {"checked": "false", "on": False}
    assert out["off"]["puts"] == [["/api/account/playback", {"auto_skip": False}]]
    assert len(out["off"]["toasts"]) == 1
    assert out["on"] == {"checked": "true", "on": True}
    assert out["puts"][-1] == ["/api/account/playback", {"auto_skip": True}]


def test_the_switch_moves_at_once_and_back_where_it_was_not_saved() -> None:
    out = _profile("fails")
    assert out["moved"] == {"checked": "false", "on": False}
    assert out["back"] == {"checked": "true", "on": True}
    assert out["toasts"] == ["error: No luck"]


def test_the_switch_shows_what_is_saved_now_not_what_the_page_was_drawn_with() -> None:
    assert _profile("fresh") == {"checked": "false", "on": False}


def test_a_second_press_before_the_answer_sends_nothing_more() -> None:
    assert _profile("twice") == 1
