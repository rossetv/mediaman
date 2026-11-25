"""The rows' arrows and keys, run under node against a fake DOM with a layout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_rows_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_paging_a_line_of_seasons_there_and_back_leaves_none_out() -> None:
    out = _run("seasons")
    for way in ("there", "back"):
        stops = out["phone"][way]
        assert sorted({n for stop in stops for n in stop}) == list(range(1, 11)), way
        # A pill wider than the line is never whole, and is no trap.
        assert {n for stop in out["wide"][way] for n in stop} == {1, 2, 3, 5, 6}, way
    assert out["phone"]["back"][-1][0] == 1


def test_a_row_is_one_tab_stop_that_leads_to_the_card_last_on_or_the_first_in_sight() -> None:
    out = _run("oneStop")
    assert out["before"] == ["row"]
    # The row itself steps aside, or Shift+Tab would come back to it.
    assert (out["inside"]["at"], out["inside"]["stops"]) == ("link 1", [])
    assert out["after"] == ["row"]
    assert (out["back"], out["scrolled"]) == ("link 2", "link 3")
    assert out["skeleton"] == []


def test_a_first_card_on_its_way_out_is_none_to_go_back_to() -> None:
    out = _run("firstGoes")
    assert out["before"] == out["gone"] == [False, True]
    assert out["going"] == [False, True]


def test_the_keys_of_a_row_are_said_once_on_the_way_in() -> None:
    out = _run("oneStop")
    assert out["inside"]["described"] == "watch-row-keys"
    assert out["moved"] == {"at": "link 2", "described": None}


def test_arrows_home_and_end_move_along_a_row_and_down_goes_to_more_and_keep() -> None:
    out = _run("keys")
    assert out["along"] == ["link 2", "link 3", "link 4", "link 3"]
    assert out["left"] == 108  # card 4 came whole into view, and card 3 was
    assert out["ends"] == ["link 6", "link 6", "link 1", "link 1"]
    assert out["down"] == ["more 1", "keep 1", "keep 2", "more 2", "link 2"]
    # Past the first and the last control, and for Tab, the browser's own.
    assert out["taken"] == [False, False, False]


def test_the_card_reached_with_end_stays_where_it_is_as_more_load_after_it() -> None:
    out = _run("steady")
    assert out["held"] == {"at": "link 8", "moved": 0, "snaps": False}
    # Touch and the pointer snap as before: only the keys' focus holds the row.
    assert (out["left"], out["taken"], out["pressed"]) == (True, True, True)
    assert (out["back"], out["keys"]) == (False, False)


def test_a_row_the_keys_leave_settles_at_a_card_s_start_before_it_snaps_again() -> None:
    out = _run("letGo")
    assert out["leaving"] == {"left": 500, "to": 540, "snaps": False}
    assert out["settled"] == {"left": 540, "to": None, "snaps": True}
    assert out["reduced"] == {"left": 540, "to": None, "snaps": True}


def test_a_row_does_not_settle_under_the_focus_or_a_finger() -> None:
    out = _run("letGo")
    # Pressed while it settles: the card stays under the pointer.
    assert out["back"] == {"left": 500, "to": None, "snaps": False}
    # Another window has the keys: the focus comes back to where it was.
    assert out["away"] == {"left": 500, "to": None, "snaps": False}
    assert out["held"] is False
    assert out["lifted"] == {"left": 333, "to": 324, "snaps": False}
    assert out["after"] == {"left": 324, "to": None, "snaps": True}


def test_cards_that_load_or_leave_neither_add_a_tab_stop_nor_lose_the_row() -> None:
    out = _run("comeAndGo")
    assert out["loaded"] == {"at": "link 4", "stops": [], "end": "link 8"}
    assert out["taken"] == ["row"]
    assert out["again"] == "link 5"
