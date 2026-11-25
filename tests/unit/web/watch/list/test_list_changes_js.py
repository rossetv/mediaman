"""Where the focus goes after an Undo in the Watch list, and which failed reads it makes good."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "list_changes_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_an_undo_gives_the_focus_back_to_the_keep_of_its_row() -> None:
    assert _run("undoFocus") == {"saved": True, "refused": True, "unread": True}


def test_an_undo_whose_row_is_not_there_puts_the_focus_on_a_row_in_sight_or_the_filter() -> None:
    assert _run("undoElsewhere") == {"page": True, "filter": "anime"}


def test_an_undo_takes_no_focus_from_where_it_is_or_from_what_is_open_over_the_list() -> None:
    assert _run("undoLeavesFocus") == {"details": True, "dialog": True, "typing": "watch-search"}


def test_a_list_that_could_not_be_read_after_a_change_is_read_when_shown_again() -> None:
    assert _run("unreadIsReadAgain") == {"reads": [1, 0], "summary": "summary now"}


def test_a_row_that_could_not_be_found_after_a_change_has_the_list_read_when_shown_again() -> None:
    out = _run("unfoundIsReadAgain")
    assert out["reads"] == 1
    assert "id:m2" not in out["keys"]


def test_a_change_during_a_load_that_failed_has_the_list_read_once_more_in_sight_or_when_shown() -> (
    None
):
    now = "summary now"
    assert _run("changeDuringAFailedLoad") == {
        "seen": {"read": 1, "errors": 1, "again": 0, "summary": now},
        "refused": {"read": 1, "errors": 1, "again": 1, "summary": now},
        "left": {"read": 0, "errors": 0, "again": 1, "summary": now},
    }


def test_a_saved_row_shows_it_though_another_list_was_asked_for_meanwhile_and_failed() -> None:
    shown = {"label": "Kept", "disabled": False, "kept": True, "summary": "summary kept"}
    assert _run("refreshBesideAFailedLoad") == {"last": shown, "first": shown}


def test_what_was_read_for_a_row_changes_nothing_in_a_list_drawn_since() -> None:
    assert _run("refreshAfterALoad") == {"summary": "summary tv", "label": "Kept"}


def test_the_focus_waits_on_the_title_of_a_row_while_a_change_to_it_is_saved() -> None:
    title, keep = "id:m2 wl-title", "id:m2 keep-trigger"
    assert _run("focusWaits") == {
        "undo": [title, keep],
        "redrawn": [title, title, keep],
        "keep": [title, keep],
        "plain": ["id:m2", keep],
    }


def test_back_between_filters_keeps_the_focus_in_the_list() -> None:
    assert _run("backBetweenFilters") == {
        "filter": "filter tv",
        "row": "id:m2 wl-title",
        "keep": "id:m2 keep-trigger",
        "delete": "id:m2 btn",
        # never the Delete of another title
        "deleteGone": "id:m9 keep-trigger",
        "rowGone": "filter ",
        "elsewhere": "watch-search",
    }


def test_back_gives_the_focus_that_waits_on_a_title_back_to_the_control_it_came_from() -> None:
    waited = ["show:77 wl-title", True]
    assert _run("backWhileTheFocusWaits") == {"keep": waited, "delete": waited, "marked": waited}


def test_back_keeps_the_focus_on_its_control_while_a_mark_of_the_row_is_saved() -> None:
    assert _run("backWhileAMarkIsSaved") == {
        "title": "id:m2 wl-title",
        "mark": "id:m2 watch-mark",
        "keep": "id:m2 keep-trigger",
        "delete": "id:m2 btn",
    }


def test_a_dialog_opened_from_a_row_gives_the_focus_back_to_the_row_as_it_is_then() -> None:
    out = _run("dialogOverAChange")
    assert out["busy"] == {"open": True, "at": "show:77 wl-title", "usable": True}
    assert out["failed"] == {"open": True, "at": "show:77 keep-trigger", "usable": True}
    # the row has left, and its neighbour has the focus
    assert out["left"] == {"open": True, "at": "show:77 keep-trigger", "usable": True}
    assert out["saved"] == {"open": True, "at": "show:77 keep-trigger", "usable": True}


def test_back_from_the_list_closes_a_dialog_opened_from_one_of_its_rows() -> None:
    assert _run("dialogOverAChange")["back"]["open"] is False
