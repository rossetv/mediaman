"""pair.js run under node against a fake DOM: the code a TV shows, and the TV's name."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

HINT = "You already have a TV with this name. You can still use it."
HARNESS = Path(__file__).parent / "js" / "pair_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_code_from_a_qr_code_pairs_nothing_until_the_person_agrees() -> None:
    out = _run("fromQr")
    assert out["before"] == {
        "sent": 0,
        "boxes": "482916",
        "agree": True,
        "readOnly": True,
        # the code stays in no address
        "replaced": ["/pair"],
    }
    assert out["sent"][0] == ["/api/tvs/pair", {"code": "482916", "confirm": False}]


def test_the_tv_is_named_after_it_is_paired() -> None:
    out = _run("fromQr")
    assert out["named"] == {
        "shown": ["pair-name-form"],
        "name": "Living room",
        "pills": ["true", "false"],
    }
    # sent once, however often it is pressed
    assert out["sent"][1:] == [["/api/tvs/7", {"name": "Bedroom TV"}]]
    assert (out["shown"], out["says"]) == (["pair-done"], "Bedroom TV is ready")
    assert out["focused"] == "pair-ready"


def test_six_numbers_are_sent_as_they_are_typed_and_a_refusal_says_why() -> None:
    out = _run("typed")
    assert out["early"] == 0
    wrong, tries, tvs, away = out["out"]
    assert wrong[0] == "482916"
    assert wrong[1][0] == "pair__why bad" and "isn’t right" in wrong[1][1] and wrong[2] == ""
    assert "five minutes" in tries[1][1]
    assert "Unpair one" in tvs[1][1]
    # what was not answered is sent again at a press
    assert (away[1], away[2]) == (["pair__why bad", "Away"], "333333")
    assert out["last"][1]["code"] == "333333" and out["shown"] == ["pair-name-form"]


def test_a_tv_of_someone_else_is_taken_over_only_after_a_yes() -> None:
    out = _run("inUse")
    assert "unpairs them" in out["asked"][0] and "stops what they’re watching" in out["asked"][0]
    assert [s["confirm"] for s in out["sent"]] == [False, True]
    assert out["shown"] == ["pair-name-form"]
    assert "stops" not in out["no"]["asked"][0]
    assert (out["no"]["sent"], out["no"]["code"], out["no"]["agree"]) == (1, "", False)


def test_a_tv_paired_again_keeps_its_name_and_another_code_can_be_scanned() -> None:
    out = _run("again")
    assert out["name"] == "Den"
    assert out["empty"] == [1, "Give it a name."]
    assert out["failed"] == [["pair-name-form"], "Away"]
    assert out["scanned"] == [["pair-form"], "111222", 2]


def test_a_new_tv_is_named_with_the_first_free_name_and_a_repeat_is_flagged() -> None:
    out = _run("twoTvs")
    assert out["shown"] == ["pair-name-form"]
    assert out["first"] == ["Living room 3", ""]
    assert out["same"][0] == "pair__why" and "already have" in out["same"][1]
    assert out["folded"] != ""
    assert out["fresh"] == ""


def test_the_name_is_still_offered_when_the_tvs_cannot_be_listed() -> None:
    out = _run("noList")
    assert (out["name"], out["shown"]) == ("Living room", ["pair-name-form"])


def test_the_name_form_shows_at_once_and_a_late_list_never_overrides_typing() -> None:
    out = _run("lateList")
    assert out["before"] == [["pair-name-form"], "Living room"]
    assert out["after"] == "Living room 2"
    assert out["typed"][0] == "Living room" and "already have" in out["typed"][1]


def test_a_list_arriving_during_a_save_leaves_the_field_as_sent() -> None:
    out = _run("listDuringSave")
    assert out["sent"] == ["/api/tvs/7", {"name": "Living room"}]
    assert out["name"] == "Living room"
    assert out["says"] == "Away"


def test_a_list_from_the_first_pairing_does_not_touch_the_second() -> None:
    out = _run("secondPairing")
    assert out == {"shown": ["pair-name-form"], "name": "Living room"}


def test_a_repeat_is_said_once_and_spaces_do_not_hide_it() -> None:
    out = _run("rewrite")
    assert out["writes"] == [HINT]
    assert out["spaced"] == HINT


def test_the_hint_keeps_two_lines_of_room_and_the_chosen_pill_shows_in_forced_colours() -> None:
    css = (HARNESS.parents[4] / "src/mediaman/web/static/css/_pair.css").read_text()
    assert re.search(r"#pair-name-why \{ min-height: 36px; line-height: 18px;", css)
    forced = css.split("@media (forced-colors: active)")[-1]
    assert "background: Highlight" in forced and "color: HighlightText" in forced


def test_a_second_press_of_save_tells_the_person_again() -> None:
    out = _run("repeatedErrors")
    assert out["failed"] == ["", "Away", "", "Away"]
    assert out["empty"] == ["", "Give it a name.", "", "Give it a name."]


def test_a_late_error_is_not_written_over_what_was_typed_or_a_save_that_went_through() -> None:
    out = _run("lateError")
    assert out["typed"] == ""
    assert out["saved"] == [["pair-done"], ""]


def test_a_malformed_list_leaves_the_name_form_as_it_was() -> None:
    out = _run("badList")
    assert out == [[["pair-name-form"], "Living room"]] * 4


def test_a_list_arriving_after_a_rescan_changes_nothing() -> None:
    out = _run("rescan")
    assert out == {"shown": ["pair-form"], "name": "Living room"}


def test_each_step_of_pairing_comes_in_and_at_once_under_reduced_motion() -> None:
    css = (HARNESS.parents[4] / "src/mediaman/web/static/css/_pair.css").read_text()
    step = r"\.pair form, #pair-done \{ animation: ([^;]+);"
    moves = re.findall(step, css)
    assert "var(--dur)" in moves[0] and moves[1] == "none"
    assert "prefers-reduced-motion" in css[: css.rindex("animation: none")]
