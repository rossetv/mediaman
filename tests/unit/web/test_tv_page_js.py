"""tv.js run under node against a fake TV browser: pairing, playing, the remote's keys, failures."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

import pytest

from tests.helpers.node_batch import run_node
from tests.unit.web._css import rules

HARNESS = Path(__file__).parent / "js" / "tv_harness.js"
STATIC = Path(__file__).resolve().parents[3] / "src/mediaman/web/static"


def _run(scenario: str, *args: str):
    result = run_node(HARNESS, scenario, *args, node_args=("--expose-internals",), timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_what_the_tv_runs_is_es5() -> None:
    assert _run("es5") == {
        "js/tv.js": "ES5",
        "js/watch/loudness.js": "ES5",
        "vendor/hls/hls.min.js": "ES5",
        "let": "refused",
    }


def test_reduce_loud_sounds_follows_whoever_started_each_title_on_the_tv() -> None:
    compressed, as_it_comes = {"dry": 0, "compressed": 1}, {"dry": 1, "compressed": 0}
    on = _run("loud", "chromium", "on")
    assert (on["first"], on["next"], on["contexts"]) == (compressed, as_it_comes, 1)
    off = _run("loud", "chromium", "off")
    # Nothing is led through Web Audio for someone who never chose it.
    assert (off["first"], off["next"]) == ("as it comes", compressed)
    # WebKit's video goes silent once led through Web Audio: there it is left alone.
    assert _run("loud", "apple", "on") == {
        "first": "as it comes",
        "next": "as it comes",
        "contexts": 0,
    }


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


def test_the_qr_code_is_named_for_a_screen_reader_as_one_picture() -> None:
    picture = _run("pairs")["picture"]
    assert 'role="img"' in picture and 'aria-label="QR code to pair this TV"' in picture
    assert 'aria-hidden="true"' in picture.split("<svg", 1)[1]


def test_a_code_that_ran_out_is_renewed_and_a_tv_nobody_stands_at_stops() -> None:
    once = _run("codeRanOut")
    assert once["asked"] == 2 and once["left"].startswith("The last code ran out.")
    out = _run("codesStop")
    assert out["asked"] == 12 and "Press OK" in out["said"]
    assert out["after"] == 13 and "111 111" in out["now"]


def test_a_tv_that_asked_for_too_many_codes_waits_as_long_as_it_is_told() -> None:
    out = _run("tooManyCodes")
    assert out["asked"] == 1 and "4 minutes" in out["first"] and "seconds" in out["said"]
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


def test_a_tv_someone_joins_keeps_its_token_and_its_film_and_says_who_can_play() -> None:
    out = _run("joined")
    assert out["src"] and out["kept"] == out["src"]
    assert (out["store"]["mm-tv"], out["more"]) == ("old", 0)
    assert "maren and tobias can play here" in out["idle"] and "482 916" not in out["idle"]
    assert out["polled"] == "old"


def test_the_code_fades_into_who_can_play_here_and_back_again() -> None:
    out = _run("joinFades")
    assert out["code"] is True
    assert out["join"] == {"says": True, "entering": "in", "left": True}
    assert out["back"] == {"entering": "in", "left": True}
    assert out["again"] == {"code": True, "entering": "in", "left": True}


def test_a_paired_tvs_code_is_for_another_person_and_a_pause_is_named_only_when_shared() -> None:
    out = _run("shared")
    assert "Pair another person" in out["code"] and "482 916" in out["code"]
    assert "Watch on this TV" not in out["code"]
    assert "Paused" in out["alone"] and "Paused by" not in out["alone"]
    assert "Paused by maren" in out["both"]
    assert out["pictured"] == ["/static/avatars/avatar-05.svg"]


def test_the_tv_names_and_shows_who_started_the_film_not_whose_tv_it_is() -> None:
    out = _run("starter")
    assert "tobias is starting it" in out["says"] and "maren is starting it" not in out["says"]
    assert out["pictures"] == ["/static/avatars/avatar-03.svg"]
    # With nobody named, the TV names nobody rather than whose TV it is.
    assert "Glass Orchard" in out["nameless"] and "starting it" not in out["nameless"]


def test_a_tv_left_open_through_an_update_loads_it_once_nothing_plays_and_once_only() -> None:
    out = _run("renewed")
    assert (out["playing"], out["idle"], out["kept"]) == (0, 1, "bbb")
    assert (out["again"], out["current"]) == (0, 0)


def test_each_person_is_shown_by_their_picture_over_their_letter_which_stays_if_it_fails() -> None:
    out = _run("faces")
    assert out["before"] == [["/api/tv/device/avatar/1/2?v=ab&k=cd", ""]]
    # Each has a letter; tobias, with no picture, has the letter alone.
    assert 'data-l="M"><img' in out["html"] and 'data-l="T"></span>' in out["html"]
    assert out["after"] == "gone"
    # The letters are drawn, not words of the page.
    assert "maren tobias" in out["says"] and " M " not in out["says"]


def test_a_film_gives_way_to_the_person_who_takes_the_tv_over() -> None:
    out = _run("takenOver")
    # what its owner starts shows over the code, which still works
    assert "482 916" in out["shown"] and out["over"]
    assert "Someone else has this TV now" in out["said"]
    assert (out["src"], out["store"]) == (None, {"mm-tv": "fresh"})
    assert "Connected to tobias" in out["then"]


@pytest.mark.parametrize("how", ["refused", "paired"])
def test_a_person_who_started_the_film_and_pairs_the_tv_anew_is_not_told_another_has_it(
    how: str,
) -> None:
    out = _run("reclaimed", how)
    assert "tobias is starting it" in out["started"]
    assert "Connected to tobias" in out["said"] and "Someone else" not in out["said"]


@pytest.mark.parametrize("n", ["7", "9"])
def test_the_people_stand_above_the_heading_alike_on_both_screens_and_seven_at_most(n) -> None:
    out = _run("crowd", n)
    for html in (out["idle"], out["conn"]):
        assert html.index('class="ppl"') < html.index('class="h"')
        assert html.count('class="pp"') == 7
    if n == "9":
        assert 'data-l="+3"' in out["idle"] and "kofi" in out["idle"] and "lena" not in out["idle"]
    else:
        assert 'data-l="+' not in out["idle"] and "lena" in out["idle"]


@pytest.mark.parametrize(
    ("n", "head"),
    [
        ("1", "Connected to maren"),
        ("2", "maren and tobias can play here"),
        ("3", "You can all play here"),
        ("9", "You can all play here"),
    ],
)
def test_a_just_paired_tv_names_two_people_and_past_that_leaves_the_names_to_their_faces(
    n: str, head: str
) -> None:
    conn = _run("crowd", n)["conn"]
    assert f'<div class="h">{head}</div>' in conn


def test_the_people_hang_from_one_line_on_both_screens() -> None:
    css = (STATIC / "css/tv.css").read_text()
    rule = re.search(r"\.idle \.mid, \.hello \.mid \{([^}]*)\}", css)
    assert rule and "padding-top: 150px" in rule[1] and "justify-content: flex-start" in rule[1]


def test_a_film_that_falters_as_it_starts_is_still_starting_not_waiting_for_the_network() -> None:
    out = _run("faltersAtStart")
    assert not [said for said in out["seen"] if "Waiting for the network" in said]
    assert "is starting it" in out["seen"][-1]
    assert "Waiting for the network" not in out["now"]


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


def test_after_a_restart_the_tv_picks_the_film_up_where_it_got_to_by_itself() -> None:
    out = _run("resumes", "restart")
    (wish,) = out["asks"]
    assert (wish["what"], wish["ticket"], wish["play_id"], wish["paused"]) == (
        "resume",
        "ticket-p1",
        "p1",
        False,
    )
    assert abs(wish["position_s"] - out["at"]) < 2
    assert "mediaman restarted" in out["said"] and "Picking up Glass Orchard" in out["said"]
    assert "is ready" not in out["then"] and out["src"] and out["playing"]
    assert abs(out["put"] - out["at"]) < 2


def test_a_film_paused_as_mediaman_restarted_comes_back_paused() -> None:
    out = _run("resumes", "paused")
    assert out["asks"][0]["paused"] is True and not out["playing"]


def test_a_film_dropped_while_the_tv_was_off_the_network_goes_on_too() -> None:
    out = _run("resumes", "network")
    assert [a["what"] for a in out["asks"]] == ["resume"]
    assert "Picking up" in out["said"] and "restarted" not in out["said"]


def test_an_update_while_a_film_plays_picks_it_up_and_loads_the_page_only_once_nothing_plays() -> (
    None
):
    out = _run("resumesUpdated")
    assert (out["asks"], out["during"], out["playing"], out["idle"]) == (1, 0, 0, 1)


@pytest.mark.parametrize("how", ["flaky", "flakyNetwork"])
def test_a_pick_up_mediaman_did_not_answer_is_asked_again_until_it_does(how: str) -> None:
    out = _run("resumes", how)
    assert [(a["what"], a["ticket"]) for a in out["asks"]] == [("resume", "ticket-p1")] * 3
    assert "is ready" not in out["said"] and "is ready" not in out["then"]
    assert out["src"] and out["playing"]


def test_a_page_opened_again_whose_ticket_ran_out_is_simply_ready() -> None:
    out = _run("resumesReopened", "expired")
    assert len(out["asks"]) == 1 and out["kept"] is None
    assert "is ready" in out["said"] and "restarted" not in out["said"]


def test_a_tv_that_wakes_from_standby_does_not_take_back_a_film_that_went_elsewhere() -> None:
    out = _run("standby")
    assert out["before"] and out["kept"] is None
    assert out["asks"] == 0 and "is ready" in out["said"]


def test_a_page_put_away_keeps_no_ticket_though_mediaman_goes_on_answering() -> None:
    out = _run("standby", "answering")
    assert out["before"] and out["kept"] is None


def test_the_ticket_kept_on_the_tv_is_as_fresh_as_its_place() -> None:
    out = _run("ticketKeptFresh")
    # Answers come every second; the kept ticket is at most a few answers old.
    assert out["given"] > 15 and int(out["kept"].split("-")[1]) >= out["given"] - 3


def test_a_film_stopped_while_the_tv_answered_does_not_start_again() -> None:
    out = _run("resumes", "stopped")
    assert out["asks"] == [] and "is ready" in out["said"]


def test_a_refused_pick_up_says_the_place_is_kept_and_asks_once() -> None:
    out = _run("resumes", "refused")
    assert len(out["asks"]) == 1 and "is kept" in out["said"] and not out["src"]


def test_a_page_opened_again_while_mediaman_was_away_picks_up_from_its_own_place() -> None:
    out = _run("resumesReopened")
    (wish,) = out["asks"]
    assert (wish["ticket"], wish["position_s"], wish["paused"]) == ("ticket-p1", 1500, True)
    assert "Picking up Glass Orchard" in out["said"]


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


def test_each_status_of_a_film_fades_into_the_next_and_none_cuts() -> None:
    for name, there, entering, left in _run("everyChangeFades"):
        assert there, name
        assert (entering, left) == ("in", True), name


def test_controls_that_were_away_come_back_in_as_a_film_is_paused() -> None:
    out = _run("pausedWhileHidden")
    assert out["before"] == "ov hide" and out["paused"]
    # Drawn hidden first, so that showing them is a fade and not a cut.
    assert out["classes"][0] == "ov hide" and out["classes"][-1] == "ov"
    # Played on, the line that said it was paused stays a moment to fade.
    assert out["line"] == "pz gone" and not out["said"]


def _seconds(value: str) -> float:
    times = re.findall(r"(?<![\w.])(\d*\.?\d+)(ms|s)\b", value)
    return sum(float(n) / (1000 if unit == "ms" else 1) for n, unit in times[:2])


def test_the_page_before_stays_until_its_fade_is_over() -> None:
    """tv.js empties the page before after LEAVE_MS: a fade longer than that would be cut off."""
    leave = int(re.search(r"var LEAVE_MS = (\d+);", (STATIC / "js/tv.js").read_text())[1])
    longest = max(
        _seconds(decls["animation"])
        for where, selectors, decls in rules("tv.css")
        if "animation" in decls
        and not where
        and any(s.startswith("#o") for s in selectors)
        and decls["animation"] != "none"
    )
    assert longest > 0 and leave >= longest * 1000


def test_the_page_before_is_there_for_its_whole_fade_and_then_gone() -> None:
    longest = max(
        _seconds(decls["animation"])
        for where, selectors, decls in rules("tv.css")
        if "animation" in decls and not where and any(s.startswith("#o") for s in selectors)
    )
    held = _run("leaves", str(round(longest * 1000)))
    assert held == {"swapped": True, "there": True}
    assert _run("leaves", "1000") == {"swapped": True, "there": False}


def test_a_picture_fades_in_with_its_shade_as_one_so_it_never_flashes() -> None:
    out = _run("artPages")
    for page in (out["starting"], out["ended"]):
        assert '<div class="pic" id="pic"><div class="shade"></div></div>' in page
        assert page.count('class="shade"') == 1


def test_the_new_page_s_first_words_rise_before_the_old_ones_are_a_quarter_gone() -> None:
    """Else the ground has covered the old words and the screen is empty a moment."""

    def timing(selector: str) -> list[float]:
        (decls,) = [
            d for w, s, d in rules("tv.css") if not w and selector in s and "animation" in d
        ]
        return [float(n) for n, _ in re.findall(r"(?<![\w.])(\d*\.?\d+)(s)\b", decls["animation"])]

    old_words = timing("#o .mid")[0]
    first_delay = timing(".in .mid > *")[1]
    assert first_delay <= old_words / 4


def test_what_moves_on_the_tv_moves_by_opacity_and_transform_alone() -> None:
    """A TV's browser hands these to its GPU; anything else repaints each frame."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "css/tv.css").read_text(), flags=re.S)
    frames = re.findall(r"@(?:-webkit-)?keyframes\s+([\w-]+)\s*\{((?:[^{}]*\{[^}]*\})*)\s*\}", css)
    assert len(frames) > 10
    for name, body in frames:
        # The icon drawing itself along its stroke, a small picture of its own.
        if name in ("a1", "a2"):
            continue
        assert set(re.findall(r"([\w-]+)\s*:", body)) <= {
            "opacity",
            "transform",
            "-webkit-transform",
        }, name


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
    assert out["later"].count('<div class="spin"><i></i></div>') == 1 and "mark" not in out["later"]


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
    assert "E5" in out["said"] and out["said"].endswith("|7")
    assert out["asks"] == [{"play_id": "p1", "what": "auto"}]


def test_the_countdown_to_the_next_episode_has_cancel_first_and_focused() -> None:
    out = _run("upnextKeys", "wait")
    assert out["focused"] == "b0" and out["line"].endswith("Cancel Play now")
    assert out["asks"] == ["auto"]


@pytest.mark.parametrize(
    ("how", "asked"), [("ok", "cancel"), ("back", "cancel"), ("right", "next")]
)
def test_ok_and_back_cancel_the_countdown_and_play_now_is_a_move_away(how: str, asked: str) -> None:
    assert _run("upnextKeys", how)["asks"] == [asked]


def test_the_countdown_says_press_to_cancel_once_and_ticks_only_its_number() -> None:
    out = _run("upnextKeys", "wait")
    assert "Next episode in 9 · Press to cancel" in out["line"].replace("&middot;", "·")
    assert "Next episode in 9 seconds. Press to cancel." in out["line"]
    assert (out["written"], out["count"], out["drain"]) == (0, "7", "9s")


def test_the_countdowns_bar_does_not_drain_under_reduced_motion() -> None:
    css = (STATIC / "css/tv.css").read_text()
    reduced = css[css.index("prefers-reduced-motion") :]
    assert re.search(r"\.up \.dr[^{]*\{[^}]*display: none", reduced)


def test_with_no_countdown_only_a_key_starts_the_next_episode_and_only_once() -> None:
    out = _run("noCountdown")
    assert "Next episode" in out["said"] and "cancel" not in out["said"]
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


def test_a_film_that_stops_for_a_moment_shows_nothing_new_over_it() -> None:
    early = _run("waits", "3000")["early"]
    assert early["pill"] != "nw on" and early["dim"] is None
    assert "Waiting" not in early["screen"]


def test_a_short_wait_is_a_pill_over_the_films_own_picture_and_goes_quietly_when_it_ends() -> None:
    out = _run("waits", "3000")
    assert (out["pill"]["pill"], out["pill"]["says"]) == ("nw on", "Waiting for the network")
    # The picture stays, held as it was, and nothing darkens it.
    assert out["pill"]["dim"] == "wl" and out["pill"]["frame"] == "on"
    assert out["back"]["pill"] == "nw" and "Back online" not in out["back"]["says"]
    # The film plays on as it was: its controls stay away, and the held picture gives way to it.
    assert out["back"]["ov"] == "ov hide" and out["back"]["frame"] == ""
    assert out["after"]["pill"] == "nw" and "Back online" not in out["after"]["says"]


def test_no_back_online_word_shows_once_the_network_is_back() -> None:
    out = _run("waits", "3000")
    for moment in ("back", "after"):
        assert "online" not in out[moment]["says"].lower()
        assert not out[moment]["pill"].startswith("nw on")


def test_only_a_long_wait_darkens_the_film_and_it_clears_as_the_film_comes_back() -> None:
    out = _run("waits", "66000")
    assert out["pill"]["dim"] == "wl"
    # Half a minute in, it is still the pill alone over the film.
    assert (out["mid"]["dim"], out["mid"]["pill"]) == ("wl", "nw on")
    assert out["long"]["dim"] == "wl on" and out["long"]["pill"] == "nw"
    assert out["back"]["dim"] is None and 'id="wl"' in out["back"]["left"]
    assert out["back"]["pill"] == "nw" and out["after"]["pill"] == "nw"


@pytest.mark.parametrize("how", ["network", "restart"])
def test_a_film_picked_up_again_waits_over_its_own_last_picture(how: str) -> None:
    out = _run("resumes", how)
    assert out["held"] == "on" and "Picking up" in out["said"]
    assert "Back online" not in out["said"]
    assert out["freed"] == "" and out["pill"] == "nw" and "Picking up" not in out["now"]


def test_the_wait_over_a_film_moves_only_what_a_weak_tv_draws_smoothly_and_stills_for_reduced_motion() -> (
    None
):
    all_rules = rules("tv.css")
    reduced: dict[str, dict[str, str]] = {}
    for w, sels, d in all_rules:
        if w and "reduce" in w[0]:
            for s in sels:
                reduced.setdefault(s, {}).update(d)
    movers = [
        (s, d)
        for w, sels, d in all_rules
        if not w
        for s in sels
        if re.match(r"\.(nw|ec|wl)\b", s) and ("transition" in d or "animation" in d)
    ]
    assert movers
    for selector, decls in movers:
        assert "filter" not in " ".join(decls), selector
        for kind in ("transition", "animation"):
            if kind not in decls:
                continue
            if kind == "transition":
                moved = re.findall(r"(?:^|,\s*)([a-z-]+)", decls[kind])
                assert set(moved) <= {"opacity", "transform", "visibility"}, selector
            assert reduced.get(selector, {}).get(kind) == "none", (selector, kind)


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


@pytest.mark.parametrize("page", ["pause", "idle", "code", "out", "ended", "err"])
def test_a_page_left_alone_dims_in_its_time_and_then_moves_its_words(page: str) -> None:
    out = _run("rests", page)
    assert out["before"] == ""
    assert all("dim" in look.split() for look in out["looks"])
    assert len(set(out["looks"])) == 4 and out["looks"][4] == out["looks"][0]


@pytest.mark.parametrize("page", ["play", "starting", "upnext", "still"])
def test_a_film_that_plays_waits_or_asks_is_never_dimmed(page: str) -> None:
    out = _run("neverRests", page)
    assert out["looks"] == []
    said = {"play": "Pause", "starting": "is starting it", "upnext": "Play now", "still": "Are you"}
    assert said[page] in out["says"]


@pytest.mark.parametrize(
    "by", ["key", "pointer", "click", "wheel", "command", "plays", "film", "closed"]
)
def test_a_dimmed_page_wakes_at_once_and_waits_its_whole_time_anew(by: str) -> None:
    out = _run("wakes", by)
    assert out["dimmed"] and out["woken"] == ""
    assert out.get("later", "") == ""


def test_a_pointer_that_lies_still_wakes_nothing() -> None:
    dimmed, still = _run("pointerLiesStill")
    assert dimmed and still == dimmed


def test_another_film_wakes_a_page_that_looks_the_same() -> None:
    dimmed, woken = _run("failsAgain")
    assert dimmed and woken == ""


@pytest.mark.parametrize("page", ["idle", "code", "ended", "err"])
def test_a_press_that_wakes_a_page_starts_nothing(page: str) -> None:
    out = _run("wakesWith", page)
    assert out["first"][0] is False
    assert out["then"] is True and out["awake"] == ""


@pytest.mark.parametrize("how", ["pause", "pausedClick"])
def test_a_press_that_wakes_a_paused_film_plays_it(how: str) -> None:
    assert _run("wakesWith", how)["first"] == [False, ["play"]]


PRESSES = ["space", "heldSpace", "tap", "heldTap"]


@pytest.mark.parametrize(
    ("page", "how"),
    [(p, h) for p in ("idle", "unpair", "ended") for h in PRESSES]
    + [(p, "key") for p in ("out", "press", "old", "conn")],
)
def test_a_press_of_any_kind_that_wakes_a_page_does_only_that(page: str, how: str) -> None:
    out = _run("pressesTwice", page, how)
    assert out["dimmed"] and out["woken"] == ""
    assert out["first"] is False and out["then"] is True


@pytest.mark.parametrize("how", ["key", "space", "tap"])
def test_a_paused_film_plays_by_the_press_that_wakes_it(how: str) -> None:
    out = _run("pressesTwice", "pause", how)
    assert out["dimmed"] and out["woken"] == ""
    assert out["calls"] == ["play"]


def test_a_click_a_while_after_a_pointer_woke_the_page_counts() -> None:
    assert _run("pointsThenClicks") == 1


def test_words_that_move_stay_in_the_safe_area_and_a_dimmed_code_can_be_read() -> None:
    css = (STATIC / "css/tv.css").read_text()
    # The 1920 px stage keeps 96 px clear at each side.
    for place, rest in ((".mid", 160), (".code", 120), (".top > div", 110), (".bot > div", 110)):
        for d in ("d1", "d2", "d3"):
            rule = re.escape(f".{d} {place}") + r"[^{]*\{[^}]* transform: translate\((-?\d+)px"
            assert abs(int(re.findall(rule, css)[-1])) <= rest - 96, (d, place)
    veil = re.search(r"\.dim:after \{[^}]*opacity: (\.\d+)", css)
    assert veil
    light = ((1 - float(veil[1]) + 0.055) / 1.055) ** 2.4
    assert (light + 0.05) / 0.05 >= 3.5


def test_a_dimmed_page_is_darkened_whole_and_each_of_its_places_is_another() -> None:
    css = (STATIC / "css/tv.css").read_text()
    veil = re.search(r"\.dim:after \{[^}]*opacity: (\.\d+)", css)
    assert veil and 0.5 <= float(veil[1]) <= 0.8
    # Unseen, and not there, over a film that plays.
    assert re.search(r"body:after \{[^}]*visibility: hidden[^}]*opacity: 0", css)
    words = (".mid", ".code", ".top > div", ".bot > div")
    moves = {
        re.search(re.escape(", ".join(f".{d} {w}" for w in words)) + r" \{ ([^}]*)\}", css)[1]
        for d in ("d1", "d2", "d3")
    }
    assert len(moves) == 3
    assert ".dim .mid" in css[css.index("prefers-reduced-motion") :]


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


def test_the_page_says_nothing_of_full_screen_and_asks_for_no_press() -> None:
    page = (STATIC / "../templates/tv.html").read_text()
    script = (STATIC / "js/tv.js").read_text()
    css = (STATIC / "css/tv.css").read_text()
    assert 'id="f"' not in page and "full screen" not in page.lower()
    assert "full screen" not in re.sub(r"//.*|/\*.*?\*/", "", script, flags=re.S).lower()
    assert ".fs" not in css


def test_a_film_starts_by_itself_and_nothing_asks_for_full_screen_before_a_press() -> None:
    out = _run("fullUnasked")
    assert out["calls"] == ["play"] and out["paused"] is False
    assert out["asks"] == 0
    assert "Press OK" not in out["said"]


def test_a_key_press_acts_and_quietly_takes_the_whole_page_full_screen_once() -> None:
    out = _run("fullByKey")
    assert out["before"] == 0 and out["polled"] == 0
    assert out["first"]["asks"] == ["root"]
    # the press did what it always does: the idle page's button asked for a code
    assert "Type this code" in out["first"]["said"]
    assert out["asks"] == 1


@pytest.mark.parametrize("where", ["button", "page"])
def test_a_click_from_a_pointing_remote_asks_for_full_screen_once(where: str) -> None:
    out = _run("fullByClick", where)
    assert out["asks"] == ["root"]
    assert out["after"] == 1
    if where == "button":
        assert "Type this code" in out["then"]
    else:
        assert out["then"] == out["said"]


def test_back_still_goes_back_and_asks_for_no_full_screen() -> None:
    out = _run("fullAndBack")
    assert "Type this code" in out["code"]
    assert "Living room is ready" in out["back"]
    assert out["asks"] == 1


def test_the_press_that_asks_for_full_screen_still_does_its_own_work() -> None:
    out = _run("fullFirstOk")
    assert out["asks"] == 1 and out["calls"] == ["pause"]


def test_a_browser_that_wants_a_key_first_plays_and_goes_full_screen_at_one_press() -> None:
    out = _run("fullFirstOk", "blocked")
    assert out["order"] == ["play", "full"]
    assert "Press OK to play" in out["said"] and "full screen" not in out["said"]
    assert out["asks"] == 1 and out["calls"] == ["play"]


def test_a_refused_request_is_made_again_only_after_a_minute() -> None:
    out = _run("fullRefused")
    assert out["refused"]["asks"] == 1 and "Type this code" in out["refused"]["said"]
    assert out["soon"] == 1
    assert out["asks"] == 2


@pytest.mark.parametrize("kind", ["webkit", "webkitOld"])
def test_a_browser_with_only_a_prefixed_api_goes_full_screen_and_asks_again_after_leaving(
    kind: str,
) -> None:
    out = _run("fullWebkit", kind)
    assert out == {"inFull": 1, "asks": ["root", "root"]}


def test_a_first_press_that_is_not_ok_acts_and_asks_for_full_screen() -> None:
    out = _run("fullFirstArrow")
    assert out["before"] == "ov hide"
    assert out["ov"] == "ov" and out["asks"] == 1


@pytest.mark.parametrize("page", ["conn", "restarted"])
def test_a_page_whose_only_button_is_harmless_goes_full_screen_and_acts_at_the_first_ok(
    page: str,
) -> None:
    out = _run("fullThenAct", page)
    assert out["asks"] == 1
    assert out["then"] != out["said"]


def test_a_file_plex_cannot_play_says_why_and_offers_no_try_again() -> None:
    said = _run("failsWith", "unplayable_dolby_vision_5")
    assert "Dolby Vision Profile 5" in said
    assert "Try again" not in said
    assert "Try again" in _run("failsWith", "unreachable")


def _declared(selector: str) -> dict[str, str]:
    """What tv.css declares for *selector* outside any @media, its rules merged in order."""
    out: dict[str, str] = {}
    for where, selectors, decls in rules("tv.css"):
        if not where and selector in selectors:
            out.update(decls)
    return out


def test_a_refusal_is_said_for_what_it_is_and_never_as_plex_out_of_reach() -> None:
    out = _run("refusals")
    assert "There is no next episode" in out["next"]
    assert "isn’t playing on the TV any more" in out["again"]
    for said in out.values():
        assert "reach Plex" not in said
    # mediaman answered the Stop, so it is not said to be silent.
    assert "isn’t answering" not in out["stop"]


def test_a_next_episode_that_cannot_start_says_so_in_place_of_a_count_stuck_at_nought() -> None:
    out = _run("autoFails")
    assert "Next episode couldn’t start" in out["failed"] and "reach Plex" in out["failed"]
    assert "Next episode in 0" not in out["failed"]
    # It goes on trying by itself, and keeps saying why while it does.
    assert out["asks"] >= 2 and "Next episode couldn’t start" in out["retried"]


def test_the_address_to_type_on_the_pairing_screen_breaks_only_after_a_dot_or_slash() -> None:
    out = _run("longAddress")
    assert out.replace("<wbr>", "") == "mediaman.vilmar-family-home.duckdns.org:8443/pair"
    assert "duckdns.<wbr>org:8443/<wbr>pair" in out
    assert "word-break" not in _declared(".addr")


def test_the_title_keeps_clear_of_the_skip_note_only_while_it_shows() -> None:
    out = _run("noteRoom")
    assert out["counting"] == {"note": True, "top": "top noted"}
    assert out["after"] == "top"
    assert "padding-right" in _declared(".top.noted")


def test_the_wait_for_a_code_counts_down_and_says_seconds_under_a_minute() -> None:
    out = _run("slowCounts")
    assert "in 3 minutes" in out["first"]
    assert "in 55 seconds" in out["later"]
    assert "a minute" not in out["later"]
    assert "in 15 seconds" in out["last"]


def test_a_long_name_under_a_face_ends_in_an_ellipsis_within_two_lines() -> None:
    pn = _declared(".pn")
    assert (pn["-webkit-line-clamp"], pn["display"], pn["max-height"]) == (
        "2",
        "-webkit-box",
        "80px",
    )
