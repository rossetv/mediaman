"""The TVs & Casting section lists the paired TVs, and asks before it unpairs one.

The real script runs under node against a small fake DOM.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "settings_tvs_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_list_holds_the_paired_tvs_each_with_rename_and_unpair_and_nothing_else() -> None:
    got = _run("list")
    assert got["rows"] == [
        ["Paired by maren. On now.", ["Rename", "Unpair"]],
        ["Paired by tobias. Not seen yet.", ["Rename", "Unpair"]],
    ]
    assert got["asked"] == ["GET /api/tvs/all"]


def test_what_chromecast_lets_in_opens_and_shuts_as_a_fold() -> None:
    assert _run("list")["folded"] == [["cast-more", "cast-more-toggle"]]


def test_no_tvs_says_so_and_nothing_more() -> None:
    said = _run("empty")
    assert said.strip() == "No TVs yet"
    assert "older" not in said.lower()


def test_a_list_that_did_not_load_can_be_tried_again() -> None:
    got = _run("unloaded")
    assert "Couldn’t load" in got["before"]
    assert got["after"] == 1


def test_a_tv_is_unpaired_only_after_a_question_and_at_its_own_address() -> None:
    got = _run("forget")
    assert (got["asked"], got["kept"]) == (False, 2)
    assert got["deleted"] == ["/api/tvs/2", "/api/tvs/1"]
    assert "No TVs yet" in got["left"]
    assert got["dropped"] == [2, 1]


def test_cancel_comes_first_in_each_panel_and_the_question_starts_on_it() -> None:
    got = _run("order")
    assert got["sure"] == [["Cancel", "Unpair this TV"], True]
    assert got["rename"][-2:] == ["Cancel", "Save"]


def test_a_tv_is_unpaired_in_the_same_word_profile_and_the_tv_use() -> None:
    got = _run("order")
    assert "Unpair" in got["row"]
    said = " ".join(got["words"])
    assert "Unpair Living room?" in said
    assert "Couldn’t unpair it." in said
    assert "forget" not in said.lower()
    assert "To use it again, pair it with a new code." in got["again"]


def test_a_rename_that_failed_keeps_what_was_typed() -> None:
    assert _run("rename") == {"failed": True, "name": "Den"}


def test_the_save_bar_saves_the_chromecast_switch_and_minds_no_tv_name_being_typed() -> None:
    got = _run("saved")
    assert (got["afterName"], got["afterEdit"]) == (False, True)
    assert got["sent"] == [{"google_cast_enabled": "true"}]


def test_cancel_fades_the_question_out_before_it_goes() -> None:
    got = _run("leaving")
    assert got["question"] == [True, "tv-sure is-leaving", True]
    assert got["gone"] is False
    assert got["reduced"] is True


def test_an_admin_unpairs_one_of_the_people_a_tv_is_paired_with() -> None:
    out = _run("people")
    assert out["before"].startswith("Paired with maren and tobias.")
    assert out["after"].startswith("Paired by maren.")
    assert (out["chips"], out["deleted"]) == (0, ["/api/tvs/1/people/tobias"])


def test_the_people_of_a_tv_fold_away_when_only_one_is_left() -> None:
    assert _run("people")["went"] == ["tv-ppl"]


def test_each_person_of_a_tv_is_shown_by_their_picture_over_their_letter() -> None:
    assert _run("faces") == [["M", "/api/avatars/3?v=ab", True], ["T", None, None]]
