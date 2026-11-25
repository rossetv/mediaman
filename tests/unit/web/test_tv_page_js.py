"""tv.js run under node against a fake TV browser: pairing, playing, the remote's keys, failures."""

from __future__ import annotations

import json
import re
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "tv_harness.js"
STATIC = Path(__file__).resolve().parents[3] / "src/mediaman/web/static"


def _run(scenario: str, *args: str):
    result = subprocess.run(
        ["node", "--expose-internals", str(HARNESS), scenario, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_what_the_tv_runs_is_es5() -> None:
    assert _run("es5") == {"js/tv.js": "ES5", "vendor/hls/hls.min.js": "ES5", "let": "refused"}


def test_the_tv_uses_nothing_its_old_browser_lacks() -> None:
    js = (STATIC / "js/tv.js").read_text()
    for new in (
        r"\bfetch\(",
        r"\bnew Promise\b|\bPromise\.",
        r"\.(closest|remove|append|includes|padStart|matches|startsWith|endsWith|finally)\(",
        r"\b(Object\.(assign|entries|values)|Array\.from|URLSearchParams|AbortController)\b",
        r"\be\.key\b",
    ):
        assert not re.search(new, js), new
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "css/tv.css").read_text(), flags=re.S)
    for new in (
        r"var\(",
        r"(?<![\w-])(gap|inset|aspect-ratio|backdrop-filter|filter)\s*:",
        r"grid|clamp\(|min\(|max\(|color-mix|:has|:is\(|:where|:focus-visible|@layer|\b(vw|vh|dvh|rem)\b",
    ):
        assert not re.search(new, css), new


def test_a_code_is_asked_for_with_no_token_and_the_new_one_is_kept_only_once_paired() -> None:
    out = _run("pairs")
    assert "mediaman.example/pair" in out["shown"] and "482 916" in out["shown"]
    assert out["left"] == "This code works for 4:57."
    assert out["asked"] == ["new"]
    assert out["before"] == {}
    assert (out["polled"], out["after"]) == ("fresh", {"mm-tv": "fresh"})
    assert "Connected to a&#38;b" in out["said"] and "<b>Den" not in out["said"]


def test_a_code_that_ran_out_is_renewed_and_a_tv_nobody_stands_at_stops() -> None:
    once = _run("codeRanOut")
    assert once["asked"] == 2 and once["left"].startswith("The last code ran out.")
    out = _run("codesStop")
    assert out["asked"] == 12 and "Press OK" in out["said"]
    assert out["after"] == 13 and "111 111" in out["now"]


def test_a_tv_that_asked_for_too_many_codes_waits_as_long_as_it_is_told() -> None:
    out = _run("tooManyCodes")
    assert out["asked"] == 1 and "4 minutes" in out["said"]
    assert "482 916" in out["now"]


def test_an_unpaired_tv_stops_the_film_and_keeps_nothing() -> None:
    out = _run("unpairedElsewhere")
    assert "Signed out" in out["said"]
    assert (out["src"], out["store"]) == (None, {})


def test_a_refused_tv_is_signed_out_only_where_its_code_is_gone_too() -> None:
    out = _run("unpairedWithACodeShown")
    assert "is ready" in out["back"]
    # the code may have been entered, and its pairing not yet be stored
    assert "482 916" in out["said"] and "Connected to tobias" in out["late"]
    assert "Signed out" in out["gone"] and out["store"] == {}


def test_a_film_gives_way_to_the_person_who_takes_the_tv_over() -> None:
    out = _run("takenOver")
    # what its owner starts shows over the code, which still works
    assert "482 916" in out["shown"] and out["over"]
    assert "Someone else has this TV now" in out["said"]
    assert (out["src"], out["store"]) == (None, {"mm-tv": "fresh"})
    assert "Connected to tobias" in out["then"]


def test_a_film_is_played_from_its_place_and_reported() -> None:
    out = _run("loads")
    assert "maren is starting it" in out["first"]
    assert (out["src"], out["loading"]) == ("/api/cast/t/index.m3u8", "loading")
    report = out["report"]
    assert report["state"] == "playing" and 103 < report["position_s"] < 106
    # the seconds played go on from those the server had
    assert 52 <= report["played_s"] <= 55
    assert (report["volume"], report["can_volume"]) == (1, True)


def test_a_page_opened_again_goes_on_from_where_it_was() -> None:
    out = _run("openedAgain")
    assert 0 <= out["behind"] <= 6
    assert out["other"] == 100
    assert out["store"] == {}


def test_stop_stops_at_once_and_locks_nothing_while_the_server_does_not_answer() -> None:
    out = _run("stopsWhileAway")
    assert out["first"]["paused"] is True and "Starting" not in out["first"]["said"]
    assert out["still"] is True
    assert "Press Stop again" in out["then"] and "Starting" not in out["then"]
    assert out["asks"] == ["stop", "stop"]
    assert out["plays"] is True and "Press Stop again" not in out["last"]


def test_no_page_has_a_style_in_its_markup_which_old_browsers_refuse() -> None:
    out = _run("noInlineStyle")
    over, left = out["over"]
    assert "style=" not in over
    # the marks of the intro's two ends and the chapter
    assert left == ["2.08%", "5.00%", "25.00%"]
    for html, picture in out["out"]:
        assert "style=" not in html
        assert picture == 'url("/api/tv/device/art/a%22b")'
    assert out["other"] == 'url("/api/tv/device/art/other")'


def test_a_browser_that_lands_off_its_place_is_put_right_twice_and_then_left() -> None:
    # once as it loads, and twice as it plays
    assert _run("landsOff") == {"sets": 3, "state": "playing", "at": True}


@pytest.mark.parametrize("how", ["plays", "paused", "slow"])
def test_no_place_is_reported_before_the_start_was_reached(how: str) -> None:
    out = _run("inertUntilItPlays", how)
    assert out["at"] and min(out["at"]) >= 1200
    assert 1200 <= out["time"] < 1210
    assert not out["kept"] or int(out["kept"].split()[1]) >= 1200


@pytest.mark.parametrize("off", ["10", "-10", "0.5"])
def test_a_film_that_says_it_plays_only_while_it_seeks_is_shown(off: str) -> None:
    out = _run("playsWhileItSeeks", off)
    assert out["report"]["state"] == "playing" and "is starting it" not in out["said"]
    assert out["sets"] <= 3 and min(out["reported"]) > 1180


def test_a_film_that_ended_with_mediaman_says_its_place_is_kept_for_a_while() -> None:
    out = _run("restarts", "waits")
    assert "mediaman restarted" in out["said"] and "Orchard is kept" in out["said"]
    assert (out["src"], out["raw"]) == (None, False)
    assert "mediaman restarted" in out["still"] and "is ready" in out["then"]


@pytest.mark.parametrize("how", ["key", "press"])
def test_any_key_goes_on_from_the_page_that_says_mediaman_restarted(how: str) -> None:
    out = _run("restarts", how)
    assert "mediaman restarted" in out["said"] and "is ready" in out["pressed"]
    # and unpairs nothing, whatever button came next
    assert "is ready" in out["then"]


def test_no_place_is_promised_in_a_film_that_was_over() -> None:
    out = _run("restarts", "over")
    assert "mediaman restarted" in out["said"] and "place" not in out["said"]


def test_only_a_film_that_ended_with_a_restart_says_so() -> None:
    assert "is ready" in _run("restarts", "same")["said"]
    assert "restarted" not in _run("restarts", "goes on")["said"]
    assert "is ready" in _run("restartsIdle")


def test_a_page_gives_way_to_the_next_and_a_page_that_stays_does_not_move() -> None:
    out = _run("pages")
    assert out["toPlay"] == ["in", False]
    assert out["cleared"] == ""
    # the controls of a film that is paused are the same place
    assert out["paused"] == ["", ""]
    assert out["idle"] == ["in", "in", True, False]
    assert out["renamed"] == [True, "", ""]


def test_the_bar_of_a_film_that_leaves_stays_where_it_was() -> None:
    out = _run("barLeaves")
    assert len(out["width"]) == 1 and 3 < float(out["width"][0].rstrip("%")) < 5
    assert out["left"] == ["25.00%", "50.00%"]
    assert out["ids"] == 0


def test_the_one_focus_follows_a_pointer_a_key_and_tab() -> None:
    out = _run("pointer")
    assert out["first"] == ["b f", "b", "b0"]
    assert out["over"] == ["b", "b f", "b1"]
    assert out["still"] == ["b f", "b", "b0"]
    assert "Unpair this TV?" in out["asks"]
    assert out["keyed"] == "b1"
    assert out["tabbed"] == ["b f", "b", "b0"]
    assert "is ready" in out["kept"]


def test_the_motion_is_off_under_reduced_motion_and_colours_forced_keep_the_focus() -> None:
    css = (STATIC / "css/tv.css").read_text()
    reduced = css[css.index("prefers-reduced-motion") :]
    for mover in (".in > div", "#o > div"):
        assert re.search(re.escape(mover) + r"[^}]*\{[^}]*animation:", css), mover
        assert mover in reduced
    forced = css[css.index("forced-colors: active") :]
    assert re.search(
        r"\.b\.f, \.bar \.p \{ forced-color-adjust: none; background: Highlight", forced
    )


def test_a_code_shown_before_counts_for_as_long_as_it_is_good() -> None:
    out = _run("usedCodeSeenLate", "used")
    assert min(out["count"]) > 20
    assert "Connected to maren" in out["said"] and out["store"] == "fresh"
    gone = _run("usedCodeSeenLate", "gone")
    assert "222 222" in gone["said"] and gone["quiet"] is True


def test_a_seek_of_a_second_is_carried_out() -> None:
    moved, acked = _run("shortSeek")
    assert -3 < moved < -0.5 and acked == 8


def test_a_clock_put_back_stops_nothing() -> None:
    out = _run("clockBack")
    assert out["told"] >= 2
    assert out["state"] == "buffering"
    assert out["asks"] == ["auto"]


def test_a_command_the_browser_refuses_is_not_repeated_and_stops_nothing() -> None:
    assert _run("badCommand") == {"more": True, "times": 1, "paused": True, "ack": 9}
    assert _run("brokenPlay") is True


def test_a_code_that_was_entered_counts_though_a_new_one_was_asked_for() -> None:
    out = _run("usedCodeAndNewCode")
    assert out["asked"] == 2
    assert "Connected to tobias" in out["said"] and out["store"] == {"mm-tv": "fresh"}


def test_the_page_waits_under_the_icon_and_a_film_that_starts_under_the_ring() -> None:
    out = _run("bootWaits")
    assert out["boot"].count('class="brand"') == 1
    assert out["boot"].count('<div class="spin spin--mark"><svg class="mark"') == 1
    assert out["later"].count('<div class="spin"></div>') == 1 and "mark" not in out["later"]


def test_a_page_that_waits_takes_no_press_for_the_page_before() -> None:
    out = _run("bootHasNoButtons")
    assert out["said"] == "mediaman" and out["asked"] == 1


@pytest.mark.parametrize("events", ["metadata", "nothing"])
def test_a_film_loaded_paused_waits_for_a_key_however_little_was_loaded(events: str) -> None:
    out = _run("pausedWithNothingLoaded", events)
    assert (out["state"], out["late"], out["loads"]) == ("paused", "paused", 0)
    assert out["at"] == 100 and out["plays"] is True


def test_each_command_is_carried_out_once_and_acked() -> None:
    out = _run("commands")
    assert out["once"] == out["again"] == ["play", "pause"]
    assert out["was"] == {"time": 900, "volume": 0.3, "muted": True}
    assert (out["time"], out["ack"], out["state"]) == (950, 11, "paused")


def test_a_film_changed_while_paused_stays_paused_and_one_asked_for_anew_plays() -> None:
    out = _run("pausedStaysPaused")
    assert out["kept"]["src"] == "/api/cast/u/index.m3u8"
    assert "play" not in out["kept"]["calls"] and out["kept"]["state"] == "paused"
    assert out["anew"][-1] == "play"


def test_a_browser_that_wants_a_key_first_says_so_and_plays_at_it() -> None:
    out = _run("needsKey")
    assert "Press OK to play" in out["said"]
    assert (out["state"], out["now"]) == ("paused", "playing")


def test_a_key_first_brings_the_controls_back_and_then_works_them() -> None:
    out = _run("keys")
    assert (out["wakes"], out["pauses"], out["back"]) == ([], ["pause"], 10)
    assert out["ask"] == {"play_id": "p1", "what": "stop"}
    assert out["active"] is True


def test_the_next_episode_is_asked_for_when_the_countdown_has_run_out() -> None:
    out = _run("upnext")
    assert out["reported"] == ["ended", 2880]
    assert "E5" in out["said"] and "Next episode in 7 s" in out["said"]
    assert out["asks"] == [{"play_id": "p1", "what": "auto"}]


def test_with_no_countdown_only_a_key_starts_the_next_episode_and_only_once() -> None:
    out = _run("noCountdown")
    assert out["said"] == "Next episode"
    assert out["asks"] == [{"play_id": "p1", "what": "next"}]


def test_still_watching_is_answered_on_the_tv() -> None:
    out = _run("still")
    assert "It stops in 9 minutes" in out["said"] and out["ask"] == "next"


def test_a_stream_that_ended_is_asked_for_again_twice_and_then_by_a_key() -> None:
    out = _run("goneTwice")
    assert out["byItself"] == 2 and "Try again" in out["said"]
    assert out["ask"] == {"play_id": "p5", "what": "again", "paused": False}


def test_a_paused_film_whose_stream_ended_stays_paused_until_a_key() -> None:
    out = _run("goneWhilePaused")
    assert out["byItself"] == {"play_id": "p1", "what": "again", "paused": True}
    assert "play" not in out["held"]
    assert out["pressed"][-1] == "play"


def test_a_picture_that_fails_is_tried_again_before_it_is_called_an_error() -> None:
    out = _run("tries")
    assert 100 <= out["at"] <= 102
    assert (out["early"], out["late"]) == ("buffering", "error")
    assert "Try again" in out["said"] and out["ask"] == "again"
    assert "three screens" in out["then"]


def test_a_browser_that_stops_short_of_the_end_has_ended() -> None:
    assert _run("stopsShort") == "ended"


def test_a_film_that_waits_says_so_and_goes_on() -> None:
    out = _run("stalls")
    assert (out["state"], out["after"]) == ("buffering", "playing")
    assert "Waiting for the network" in out["said"]


def test_a_server_that_does_not_answer_shows_only_where_nothing_plays() -> None:
    out = _run("lost")
    assert "answering" not in out["whilePlaying"]
    assert "answering" in out["stuck"] and "answering" in out["idle"]
    assert out["kept"] == {"mm-tv": "old"} and "is ready" in out["back"]


def test_the_tv_unpairs_itself_only_after_a_yes() -> None:
    out = _run("unpairs")
    assert "Unpair this TV?" in out["asks"] and "maren won&rsquo;t" in out["asks"]
    assert out["before"] == 0
    assert (out["sent"], out["store"], out["quiet"]) == (["old"], {}, True)


def test_names_and_titles_are_never_markup() -> None:
    pages = _run("escapes")
    assert sum("&#60;img" in html for html in pages) == 5
    assert not any("<img" in html or 'url(&quot;/x"' in html for html in pages)


def test_a_browser_that_cannot_play_is_told_before_it_pairs() -> None:
    out = _run("cannotPlay")
    assert "can&rsquo;t play films" in out["said"] and out["sent"] == 0
    assert "482 916" in out["then"]


@pytest.mark.parametrize(
    ("text", "side", "digest"),
    [
        # Each was read back by a QR reader when it was written down.
        ("http://tv/p#1", 21, "6a46770a2096e3b2"),
        ("https://mediaman.example/pair#482916", 29, "870baf6fe8c57898"),
        ("https://mediaman.example/" + "a" * 90 + "/pair#482916", 49, "10c60fe742fa7c85"),
        ("https://mediaman.example/" + "a" * 180, 57, "e397f0e358f05bba"),
    ],
)
def test_the_qr_code_is_the_one_a_camera_reads(text: str, side: int, digest: str) -> None:
    rows = _run("qr", text)
    assert (len(rows), {len(row) for row in rows}) == (side, {side})
    assert sha256("".join(rows).encode()).hexdigest()[:16] == digest


def test_an_address_too_long_for_a_code_has_none() -> None:
    assert _run("qr", "x" * 214) is None
