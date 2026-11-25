"""pair.js run under node against a fake DOM: the code a TV shows, and the TV's name."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "pair_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
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
