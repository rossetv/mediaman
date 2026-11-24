"""ui-feedback.js run under node against a fake DOM: when Ctrl+Z presses a toast's Undo,
and confirms asked together."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "ui_feedback_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False, timeout=20
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_dismissing_answers_every_confirm_still_open_and_no_other() -> None:
    assert _run("stacked") == [
        {"ids": 2, "answers": [False, True]},
        {"ids": 2, "answers": [True, False]},
        {"ids": 2, "answers": [False, False]},
    ]


def test_a_confirm_is_named_by_its_title_and_described_by_its_body() -> None:
    assert _run("named") == {"body": ["Delete it?", "For good."], "none": ["Delete it?", None]}


def test_a_confirm_is_not_answered_by_the_rest_of_a_double_click_on_what_asked() -> None:
    out = _run("stray")
    assert out["doubleClick"] == [None, None, None]
    # nor yes, or no by its backdrop, before it could be read; Cancel is always safe
    assert out["early"] == [None, None, False]
    assert out["meant"] == [True, False, False]
    assert out["byKey"] == [True, False]


def test_a_confirm_can_be_answered_after_the_clock_was_put_back_under_it() -> None:
    assert _run("stray")["meant"] == [True, False, False]


def test_a_confirm_is_answered_by_a_run_of_clicks_that_began_in_it() -> None:
    out = _run("stray")
    assert out["run"] == [True, True, False]
    # but still not before it could be read, nor by a run that began on what asked
    assert out["soon"] == [None, None]
    assert out["strayRun"] == [None, None, None]


def test_a_long_unbroken_name_wraps_in_a_confirm_s_title() -> None:
    css = (HARNESS.parents[4] / "src/mediaman/web/static/css/_ui_feedback.css").read_text()
    title = css.split(".ui-confirm-title {")[1].split("}")[0]
    assert "overflow-wrap: anywhere" in title


def test_a_confirm_holds_the_width_of_the_page_s_scrollbar_until_it_closes() -> None:
    assert _run("bar") == {"held": "15px", "after": None}


def test_an_error_stays_while_the_pointer_is_on_it_and_goes_once_it_has_left() -> None:
    assert _run("held") == {
        "error": [True, False],
        "errorAfterATap": [False, False],
        "plain": [False, False],
    }


def test_undo_answers_ctrl_z_unless_a_dialog_on_screen_or_a_text_field_has_the_keyboard() -> None:
    assert _run("pressed") == {
        "nothingOpen": True,
        # a dialog in something that is not shown
        "closedInside": True,
        "open": False,
        # a toast from the dialog that is open
        "itsOwn": True,
        "anotherOverItsOwn": False,
        "confirmOverItsOwn": False,
        "textFieldInItsOwn": False,
        "ariaHidden": True,
        "invisible": True,
        "notModal": True,
        "confirm": False,
        "textField": False,
        "redo": False,
    }


def test_toasts_are_inside_whatever_is_full_screen_and_come_back_with_their_focus() -> None:
    assert _run("fullscreen") == {
        "shownIn": "player",
        "after": "body",
        "focusKept": True,
        "webkit": "player",
        "video": "body",
        "hosts": 1,
    }


def test_at_most_two_toasts_on_a_phone_where_a_new_undo_takes_the_place_of_the_oldest() -> None:
    assert _run("onPhone") == {
        "wide": ["b", "c", "d"],
        # Elsewhere an Undo keeps its time.
        "wideUndo": ["1 Undo", "2 Undo", "3 Undo", "4 Undo"],
        "phone": ["b", "c"],
        "phoneUndo": ["2 Undo", "3 Undo"],
        "undoStays": ["1 Undo", "b"],
        "errorStays": ["1 Undo", "2 Undo"],
    }
