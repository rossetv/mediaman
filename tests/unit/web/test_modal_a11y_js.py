"""modal-a11y.js run under node against a fake DOM: what is out of reach behind a dialog, where
Tab and Escape go, and what has the keyboard first."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "modal_a11y_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False, timeout=20
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_page_behind_a_confirm_is_inert_but_for_the_toasts_and_is_given_back() -> None:
    out = _run("confirm")
    assert out["before"] == ["closed-panel"]
    assert out["open"] == ["top", "main", "closed-panel", "low", "plain"]
    assert out["closed"] == ["closed-panel"]
    assert out["focused"] == "opener"


def test_a_dialog_inside_the_main_part_leaves_only_itself_in_reach() -> None:
    out = _run("inside")
    assert out["open"] == ["top", "list", "closed-panel", "low", "plain"]
    assert out["closed"] == ["closed-panel"]
    assert out["again"] == ["closed-panel"]


def test_toasts_shown_over_what_is_full_screen_stay_usable_under_a_dialog() -> None:
    out = _run("toastsFullScreen")
    assert "list" not in out and "main" not in out
    assert {"top", "opener", "inside"} <= set(out)


def test_dialogs_over_one_another_give_the_page_back_with_the_last_in_either_order() -> None:
    out = _run("stacked")
    behind = ["top", "main", "plain"]
    assert out["newest"] == {
        "one": {"inert": behind, "focused": "low-no"},
        "two": ["top", "main", "low", "plain"],
        "mid": {"inert": behind, "focused": "low-no"},
        "end": {"inert": [], "focused": "opener"},
    }
    assert out["oldest"]["mid"] == {"inert": ["top", "main", "low", "plain"], "focused": "Cancel"}
    # the confirm was opened from the dialog that has gone
    assert out["oldest"]["end"] == {"inert": [], "focused": "opener"}
    assert out["plain"]["one"]["inert"] == ["top", "main", "low"]
    assert out["plain"]["mid"]["inert"] == ["top", "main", "low"]
    assert out["plain"]["end"]["inert"] == []


def test_a_dialog_with_its_own_focus_has_the_page_back_before_it_hands_the_focus_on() -> None:
    assert _run("giveBackFirst") == {
        "open": ["top", "main", "closed-panel", "low"],
        "closed": ["closed-panel"],
        "focused": "opener",
    }


def test_a_dialog_closed_at_once_does_not_take_the_focus_afterwards() -> None:
    assert _run("closedAtOnce") == "opener"


def test_tab_goes_round_every_control_in_the_dialog_and_never_by_the_browser_s_own_step() -> None:
    out = _run("tab")
    assert out["on"] == ["field", "low-yes", "low-no"]
    assert out["back"] == ["low-yes", "field", "low-no"]
    assert out["heading"] == ["low-no", "low-yes"]
    assert out["nowhere"] == ["low-no", "low-yes"]
    assert out["toast"] == ["low-no"]
    assert out["unseen"] == ["low-yes"]
    assert out["none"] == ["low"]


def test_tab_stops_where_the_browser_would_and_nowhere_else() -> None:
    assert _run("stops") == ["first", "more", "frame", "own", "first"]


def test_the_bar_of_a_tv_is_in_reach_under_a_titles_page_and_no_other_dialog() -> None:
    out = _run("tvBar")
    assert out["otherDialog"] is True
    assert out["inert"] == ["top", "main", "low", "plain"]
    assert out["tab"] == ["watch", "remote", "pause", "shut"]
    assert out["back"] == ["pause", "remote"]
    assert out["under"] is True
    assert out["underTab"] == ["field", "low-yes", "low-no"]
    assert out["hidden"] == ["watch", "shut"]


def test_tab_stops_once_at_each_group_of_radios() -> None:
    assert _run("radios") == {
        "none": ["a1", "b1", "after", "before"],
        "checked": ["a3", "b1", "after", "before"],
        "back": ["after", "b1", "a3", "before"],
        "from": ["b1", "after", "before", "a3"],
    }


def test_a_dialog_that_left_the_page_before_it_closed_gives_the_page_and_the_focus_back() -> None:
    out = _run("goneFromThePage")
    assert out["alone"] == {"inert": [], "focused": "opener"}
    assert out["over"] == {"inert": ["top", "main", "plain"], "focused": "low-no"}
    assert out["after"] == {"inert": [], "focused": "opener"}


def test_closing_what_a_part_of_the_page_opened_closes_what_that_opened_and_nothing_else() -> None:
    out = _run("closeFrom")
    assert out["within"] == {"left": [False, 0], "focused": "opener"}
    assert out["elsewhere"] == {"left": [True, 1], "focused": "Cancel"}
    assert out["gone"]["left"] == [False, 0]


def test_tab_stays_in_a_dialog_with_its_own_focus() -> None:
    assert _run("tabInPlain") == ["plain-yes", "password"]


def test_escape_closes_only_the_dialog_on_top() -> None:
    assert _run("escape") == {"once": ["low"], "twice": []}


def test_the_first_focus_is_on_something_that_can_be_seen() -> None:
    assert _run("first") == {
        "marked": "heading",
        "hidden": "low-no",
        "invisible": "low-no",
        "nextMarked": "low-yes",
        "firstUnseen": "field",
        "nothing": "low",
    }


def test_tab_in_a_menu_over_a_dialog_goes_on_from_the_button_it_was_opened_from() -> None:
    assert _run("menuOver") == "field"


def test_the_focus_goes_back_to_what_opened_a_dialog_without_scrolling_the_page() -> None:
    assert _run("scrolls") == {"preventScroll": True}


def test_what_a_dialog_spares_is_hidden_from_assistive_tech_not_made_inert_and_given_back() -> None:
    out = _run("spared")
    assert out["open"] == {"inert": ["closed-panel", "low", "plain"], "hidden": ["top", "list"]}
    # a confirm over it makes no more of the page inert than what it would itself
    assert "list" not in out["over"]["inert"] and "top" not in out["over"]["inert"]
    assert {"top", "list"} <= set(out["over"]["hidden"])
    assert out["closed"] == {"inert": ["closed-panel"], "hidden": [], "top": "false"}
