"""The TVs & Casting section shows each state of a check, and asks before it forgets a TV.

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


def test_the_list_holds_paired_and_older_tvs_and_only_older_ones_are_checked() -> None:
    assert _run("list")["rows"] == [
        ["Paired by maren. On now.", False],
        ["Older TV at 192.168.1.44. Not seen yet.", True],
    ]


def test_no_tvs_says_how_to_get_one() -> None:
    assert "No TVs yet" in _run("empty")


def test_a_list_that_did_not_load_can_be_tried_again() -> None:
    got = _run("unloaded")
    assert "Couldn’t load" in got["before"]
    assert got["after"] == 1


def test_a_tv_is_added_only_once_found_and_its_name_is_only_text() -> None:
    got = _run("found")
    assert got["checking"] == ["ok", "run", "todo", "todo"]
    assert got["early"] is True
    assert got["showing"] == [["ok", "ok", "ok", "run"], True]
    where = "/api/settings/older-tvs/picture"
    assert got["picture"] == ["POST", where, {"address": "192.168.1.44"}]
    assert (got["found"], got["from"]) == (["ok", "ok", "ok", "ok"], True)
    assert got["text"] is True
    body = {"address": "192.168.1.44", "name": "Attic"}
    assert got["sent"] == ["POST", "/api/settings/older-tvs", body]
    assert (got["rows"], got["shut"]) == (3, True)


def test_another_address_must_be_checked_again() -> None:
    assert _run("edited") == {"ready": True, "after": True}


def test_a_late_answer_for_an_address_no_longer_typed_counts_for_nothing() -> None:
    assert _run("late") is True


def test_a_name_or_internet_address_is_refused_without_asking() -> None:
    assert _run("typed") == {"states": ["no"], "asked": False}


def test_each_failure_says_in_plain_words_what_to_try() -> None:
    got = _run("failures")
    assert {state for state, _ in got.values()} == {"no"}
    assert "Is the TV on, on the same home network" in got["unreachable"][1]
    assert "pairing with a code" in got["not_a_tv"][1]
    assert "Host networking, macvlan" in got["own_network"][1]
    assert "said off" in got["off"][1]


def test_a_tv_that_cannot_fetch_the_test_picture_is_not_added_and_says_why() -> None:
    got = _run("pictures")
    assert all(states == ["ok", "ok", "ok", "no"] for states, *_ in got.values())
    assert "must start with http://" in got["no_fetch"][1]
    assert "Set the address TVs use" in got["no_tv_address"][1]
    assert "Turn on Plain http" in got["http_off"][1]
    assert "couldn’t reach mediamansaid host_not_allowed" in got["host_not_allowed"][1]
    assert "said busy" in got["busy"][1]
    # A TV that shows no pictures may still play films.
    assert [code for code, (_, _, shut, _) in got.items() if not shut] == ["no_picture"]
    assert "said no_picture You can still add it" in got["no_picture"][1]
    assert "<b>TV</b> did" in got["no_fetch"][3]
    assert " did" not in got["busy"][3]


def test_checking_a_kept_tv_shows_whether_it_answered() -> None:
    got = _run("checked")
    assert got["off"] == ["ok", "no"]
    assert got["on"] == ["ok", "ok"]
    assert "Last seen today" in got["says"]
    assert "Older TV at 192.168.1.77" in got["says"]


def test_a_tv_is_forgotten_only_after_a_question_and_each_kind_at_its_own_address() -> None:
    got = _run("forget")
    assert (got["asked"], got["kept"]) == (False, 2)
    assert got["deleted"] == ["/api/settings/older-tvs/2", "/api/tvs/1"]
    assert "No TVs yet" in got["left"]


def test_a_rename_that_failed_keeps_what_was_typed() -> None:
    assert _run("rename") == {"failed": True, "name": "Den"}


def test_an_address_no_tv_can_use_is_marked_as_typed() -> None:
    local, https, http = _run("address")
    assert local[0] == "true"
    assert "localhost" in local[1]
    assert https[0] is None
    assert "https" in https[1]
    assert "Plain http" in http[1]


def test_the_save_bar_saves_the_settings_and_minds_nothing_typed_to_add_a_tv() -> None:
    got = _run("saved")
    assert (got["afterAdd"], got["afterEdit"]) == (False, True)
    assert got["sent"] == [
        {
            "older_tvs_enabled": "true",
            "older_tvs_http": "true",
            "tv_address": "http://192.168.1.10:8282",
        }
    ]


def test_a_line_of_a_check_that_stands_does_not_come_in_again() -> None:
    got = _run("stands")
    home = "192.168.1.44 is on your home network"
    assert got["add"][0] == ["ok", home]
    assert got["add"][1][1] == "Can play films from mediaman"
    assert got["row"] == [["ok", home]]


def test_cancel_fades_the_question_and_the_add_panel_out_before_they_go() -> None:
    got = _run("leaving")
    assert got["question"] == [True, "tv-sure is-leaving", True]
    assert got["gone"] is False
    assert got["panel"][0] is False
    assert "is-leaving" in got["panel"][1]
    assert got["panel"][2] is False
    assert got["again"][0] is False
    assert "is-leaving" not in got["again"][1]
    assert got["again"][2] is False
    assert got["shut"] == [True, 0]
    assert got["reduced"] is True
