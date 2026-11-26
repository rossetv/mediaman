"""A finger held on a Watch card's poster opens its preview, run under node against a fake DOM
and a fake clock. One node run gives every scenario's outcome."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "watch_preview_finger_harness.js"
CSS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch_cards.css"


@pytest.fixture(scope="module")
def out() -> dict:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_held_finger_opens_a_phone_wide_preview_that_stays_without_its_trailer(out) -> None:
    assert out["held"] == {
        "open": True,
        "width": "366px",
        "finger": True,
        "opened": [],
        "trailer": False,
        "hint": False,
    }


def test_the_preview_keeps_twelve_pixels_each_side_on_a_small_phone_and_stops_growing_on_a_big_one(
    out,
) -> None:
    assert (out["heldOnANarrowPhone"], out["heldOnAWidePhone"]) == ("296px", "400px")


def test_a_tap_opens_the_title_and_asks_for_no_preview(out) -> None:
    assert out["tapped"] == {"open": False, "opened": ["film"], "asked": []}


def test_a_finger_that_moves_is_cancelled_or_scrolled_away_opens_nothing(out) -> None:
    assert out["moved"] == {"moved": False, "wobbled": True, "cancelled": False, "scrolled": False}


def test_a_finger_that_stops_a_gliding_row_opens_nothing(out) -> None:
    assert out["onAGlidingRow"] is False


def test_lifting_the_finger_leaves_the_preview_open(out) -> None:
    assert out["released"] is True


def test_a_tap_outside_closes_it_and_opens_nothing_and_the_next_tap_opens_its_title(out) -> None:
    assert out["closedByATapOutside"] == {"open": False, "opened": ["other"], "layers": 0}


def test_a_tap_inside_the_preview_does_not_close_it(out) -> None:
    assert out["tapInsideStays"] is True


def test_a_scroll_closes_it(out) -> None:
    assert out["scrolledAway"] is False


def test_where_it_cannot_fit_or_its_reply_fails_the_hold_opens_the_title(out) -> None:
    assert out["noRoom"] == {"open": False, "opened": ["film"]}
    assert out["replyFailed"] == {"open": False, "opened": ["film"]}


def test_the_phones_own_menu_is_kept_off_a_card_by_touch_and_not_by_mouse(out) -> None:
    assert out["phoneMenu"] == {"touch": True, "mouse": False}


def test_a_page_staged_under_the_preview_is_ended_when_its_motion_cannot_be_made(out) -> None:
    assert out["motionThrows"] == ["draw movie-1", "end 0", "undraw"]


def test_the_phones_callout_and_text_selection_are_kept_off_cards_by_touch() -> None:
    css = CSS.read_text()
    blocks = css.split("@media (hover: none), (pointer: coarse) {")[1:]
    rules = [line for b in blocks for line in b[: b.index("\n}\n")].splitlines()]
    link = [r for r in rules if r.strip().startswith(".watch-card__link {")]
    assert link and "-webkit-touch-callout: none" in link[0] and "user-select: none" in link[0]


def test_card_titles_keep_their_own_look_over_the_poster_in_forced_colours() -> None:
    css = CSS.read_text()
    forced = css[css.index("@media (forced-colors: active) {") :]
    forced = forced[: forced.index("\n}\n")]
    assert ".watch-card__title" in forced
    assert "forced-color-adjust: none" in forced
