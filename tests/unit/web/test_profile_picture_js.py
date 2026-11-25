"""profile-picture.js run under node against a fake DOM: picking, placing and saving a picture."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

HARNESS = Path(__file__).parent / "js" / "profile_picture_harness.js"


def _run(scenario: str) -> Any:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_drawn_picture_is_saved_once_and_shown_at_once() -> None:
    out = _run("pick")
    assert out["first"] == {"state": ["Your default picture", True, True], "checked": "2"}
    assert out["picked"] == {"state": ["Your pick", False, False], "now": "/d3", "sentYet": 0}
    assert out["sent"][0] == ["PUT", "/api/account/avatar", {"choice": 3}]
    assert out["saved"] == {
        "open": False,
        "told": [["mm:avatar", "/d3"]],
        "toasts": ["Picture saved."],
        "head": "/d3",
    }
    # opened again, what was saved is what is kept
    assert out["again"] == {"state": ["Your pick", False, True], "checked": "3"}


def test_a_removed_picture_goes_back_to_the_default() -> None:
    out = _run("pick")
    assert out["removed"] == ["Your default picture", True, False]
    assert out["sent"][1:] == [["DELETE", "/api/account/avatar"]]
    assert out["last"] == {"told": [["mm:avatar", "/d2"]], "toasts": ["Picture removed."]}


def test_cancelling_keeps_the_picture() -> None:
    out = _run("cancel")
    assert out == {
        "open": False,
        "state": ["Your default picture", True, True],
        "checked": "2",
        "told": [],
    }


def test_a_file_that_cannot_be_used_is_refused_in_words() -> None:
    out = _run("files")
    for name, words in {
        "gif": "JPEG, PNG or WebP",
        "big": "That photo is 14 MB. The most is 10 MB.",
        "justOver": "That photo is 10.1 MB. The most is 10 MB.",
        "small": "That photo is 90 × 72 pixels. It needs at least 128 each way.",
        "broken": "not a photo",
        "heicNot": "This browser cannot open HEIC photos",
    }.items():
        said, placing, kept = out[name]
        assert words in said and not placing and kept == 0, name


def test_a_photo_the_browser_opens_is_placed_heic_too() -> None:
    out = _run("files")
    assert out["good"] == out["heicHere"] == ["", True, 0]


def test_the_photo_moves_by_keys_and_always_fills_its_circle() -> None:
    out = _run("place")
    # 576 across in a view of 640: an 800 by 600 photo is 768 wide, with 96 to spare each side
    assert out["first"] == [-64, 32, 768, 576]
    assert out["left"][0] == out["first"][0] + 16
    assert out["stuck"] == out["left"]
    assert out["edge"][0] == 640 - 32 - 768


def test_the_photo_is_zoomed_within_its_limits_by_keys_pinch_and_wheel() -> None:
    out = _run("place")
    assert (out["most"], out["least"]) == (400, 100)
    # the browser's own zoom is left to it
    assert out["browsers"] == 400
    assert out["dragged"] == 20
    assert out["pinched"] == out["released"] == 200
    assert out["wheeled"] is True


def test_the_square_around_the_circle_is_cut_out_small_as_a_jpeg_or_a_png() -> None:
    out = _run("cut")
    assert out["plain"]["from"] == [100, 0, 600, 600]
    assert (out["plain"]["size"], out["plain"]["type"]) == ([512, 512], "image/jpeg")
    # never under what the server takes
    assert (out["small"]["size"], out["small"]["type"]) == ([256, 256], "image/png")
    assert out["huge"]["work"] == [4096, 2731]
    assert out["huge"]["type"] == "image/png"
    for cut in out.values():
        assert (cut["state"], cut["placing"], cut["focus"]) == (
            ["Your photo", False, False],
            False,
            True,
        )


def test_a_photo_is_sent_as_it_is_and_shown_at_once() -> None:
    out = _run("photo")["saved"]
    assert out["sent"] == [["PUT", "/api/account/avatar/photo", "image/jpeg", "image/jpeg"]]
    assert out["told"] == [["mm:avatar", "/api/avatars/1?v=x"]]
    assert (out["open"], out["toasts"], out["left"]) == (False, ["Picture saved."], 0)


@pytest.mark.parametrize(
    ("case", "words"),
    [
        ("refused", "Too small."),
        ("proxy", "That photo is too big. Choose a smaller one."),
        ("broken", "Could not save your picture. Try again."),
        ("offline", "Could not reach mediaman. Check your connection and try again."),
    ],
)
def test_a_photo_that_was_not_saved_says_why_and_can_be_tried_again(case, words) -> None:
    out = _run("photo")[case]
    assert (out["error"], out["open"], out["told"]) == (words, True, [])
    assert (out["label"], out["state"]) == ("Save", ["Your photo", False, False])


def test_a_failure_after_the_dialog_was_closed_is_still_said() -> None:
    assert _run("photo")["left"]["toasts"] == ["error: Too small."]


def test_a_photo_sent_when_signed_out_goes_to_the_sign_in_page() -> None:
    out = _run("photo")["signedOut"]
    assert (out["redirects"], out["error"]) == (1, "")


def test_a_photo_the_browser_could_not_cut_is_said() -> None:
    out = _run("photo")["unmade"]
    assert out["error"] and out["sent"] == []


def test_a_drag_that_ends_on_the_backdrop_does_not_close_the_dialog() -> None:
    assert _run("backdrop") == [True, False]


def test_a_dialog_opened_again_while_a_photo_is_saved_shows_that_one_and_removes_nothing() -> None:
    out = _run("reopened")
    assert out["after"] == {"open": True, "state": ["Your photo", False, True], "now": "/new"}
    assert out["sent"] == ["PUT /api/account/avatar/photo"]


def test_nothing_else_is_chosen_while_a_picture_is_saved() -> None:
    out = _run("locked")
    assert out["before"] == out["after"] == [True, True, True, True]
    # it can still be closed
    assert out["during"] == [False, False, False, True]


def test_a_photo_being_read_is_waited_for_before_anything_is_saved() -> None:
    out = _run("locked")
    assert (out["reading"], out["placing"], out["thenSave"]) == (True, True, False)


def test_an_answer_that_names_no_picture_is_not_taken_for_a_saved_one() -> None:
    assert (
        _run("notJson")
        == [
            {
                "error": "Could not save your picture. Try again.",
                "told": [],
                "toasts": [],
                "head": "/d2",
                "open": True,
            }
        ]
        * 2
    )


def test_the_height_follows_from_picking_to_placing_and_back() -> None:
    assert _run("height") == {
        "full": [["417px", "562px", True], ["562px", "417px", False]],
        "calm": [],
    }


def test_the_line_of_the_zoom_is_filled_as_far_as_it_is_zoomed() -> None:
    assert _run("fill") == [0, 0.5]


def test_a_reason_comes_and_goes_by_the_height_and_is_brought_into_view() -> None:
    assert _run("errorHeight") == {
        "runs": [["417px", "491px"], ["491px", "417px"]],
        "shown": [["pic-err", "nearest"]],
    }


def test_what_is_brought_into_view_stops_under_the_header_that_stays() -> None:
    css = (HARNESS.parents[4] / "src/mediaman/web/static/css/_profile_picture.css").read_text()
    room = re.search(r"\.pic \{[^}]*scroll-padding-top: (\d+)px", css)
    # the header: its padding of 18px at most and a close button of 44px
    assert room and int(room[1]) >= 18 + 44
