"""nav.js run under node against a fake DOM: the profile menu and its phone sheet."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "nav_harness.js"
NAV_CSS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_nav.css"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_avatar_opens_the_menu_on_its_first_item_and_closes_it_again() -> None:
    out = _run("toggle")
    assert out["opened"] == {
        "open": True,
        "expanded": "true",
        "focused": "account",
        "inert": ["already-inert"],
    }
    assert out["closed"] == {"open": False, "expanded": "false", "focused": "button"}


def test_arrows_home_and_end_move_round_the_menu_and_escape_returns_focus() -> None:
    out = _run("keys")
    assert out["trail"] == [
        "account",
        "settings",
        "logout",
        "account",
        "logout",
        "account",
        "logout",
    ]
    assert out["open"] is False
    assert out["focused"] == "button"
    # the page's own Escape, such as closing a title, must not act as well
    assert out["escStopped"] is True


def test_arrow_up_on_the_avatar_opens_the_menu_at_its_last_item() -> None:
    assert _run("upOpensAtTheEnd") == {"focused": "logout"}


def test_the_space_bar_follows_a_menu_link_instead_of_scrolling_the_page() -> None:
    assert _run("space") == {"clicks": 1, "prevented": True}


def test_tab_closes_the_menu_and_goes_on_from_the_avatar() -> None:
    assert _run("tab") == {"open": False, "focused": "button", "prevented": False}


def test_the_skip_link_pressed_again_on_the_same_address_scrolls_to_the_content() -> None:
    out = _run("skip")
    # the first press is the browser's own: it sets the address, moves focus and scrolls
    assert out["first"] == {"prevented": False, "scrolled": 0, "focused": "elsewhere"}
    assert out["again"] == {"prevented": True, "scrolled": 1, "focused": "main-content"}
    # a new tab, a middle click and an address that differs are left to the browser
    assert out["ctrl"] == out["middle"] == out["otherAddress"] == out["first"]


def test_a_press_outside_focus_leaving_back_and_a_followed_link_close_the_menu() -> None:
    assert _run("leaving") == {
        "press": False,
        "focus": False,
        "inside": True,
        "back": False,
        "link": False,
    }


def test_the_phone_sheet_holds_the_page_still_and_out_of_reach_until_it_closes() -> None:
    out = _run("sheet")
    assert out["held"] == {
        "inert": ["top", "main", "already-inert"],
        "scrolls": False,
        "focused": "account",
    }
    assert out["freed"] == {
        "open": False,
        "inert": ["profile-menu", "already-inert"],
        "scrolls": True,
        "focused": "button",
    }


def test_a_dialog_over_the_phone_sheet_keeps_the_page_out_of_reach_as_the_sheet_closes() -> None:
    assert _run("dialogOverSheet") == {
        "over": {
            "open": False,
            "inert": ["top", "profile-menu", "main", "already-inert"],
        },
        "freed": ["already-inert"],
    }


def test_the_phone_sheet_has_a_button_that_closes_it() -> None:
    assert _run("closeButton") == {
        "open": False,
        "inert": ["profile-menu", "already-inert"],
        "scrolls": True,
        "focused": "button",
    }


def test_giving_the_focus_back_to_a_button_in_the_bar_never_scrolls_the_page() -> None:
    assert _run("focusBack") == {
        key: [True] for key in ("escape", "tab", "close", "button", "resize")
    }


def test_outgrowing_the_phone_layout_closes_the_sheet_and_gives_the_avatar_the_focus() -> None:
    assert _run("resize") == {
        "open": False,
        "inert": ["profile-menu", "already-inert"],
        "scrolls": True,
        "focused": "button",
    }


def test_a_closed_menu_is_inert_while_it_fades_and_reopening_undoes_it_at_once() -> None:
    assert _run("fade") == {
        "fresh": False,
        "opened": False,
        "closed": {"inert": True, "focused": "button"},
        "reopened": {"inert": False, "focused": "account"},
        "sheet": {"inert": True, "focused": "button"},
    }


def test_a_changed_avatar_takes_the_old_one_s_place_once_loaded_and_the_last_change_wins() -> None:
    assert _run("avatar") == {
        "loading": [None, None],
        "stale": [None, None],
        "shown": ["/new", "/new"],
        "initials": "",
    }


def test_with_reduced_motion_nothing_in_the_menu_is_still_hidden_when_it_takes_the_focus() -> None:
    reduce = NAV_CSS.read_text().split("(prefers-reduced-motion: reduce) {")[1].split("\n}")[0]
    still = [
        head
        for head, body in re.findall(r"([^{}]+)\{([^}]*)\}", reduce)
        if "transition: none" in body
    ]
    assert any(".profile-menu *" in head for head in still)


def test_the_ring_the_count_and_the_menu_say_how_what_is_downloading_stands() -> None:
    out = _run("downloads")
    name = "Profile menu, robin"
    assert out["none"] == {
        "name": name,
        "ring": [],
        "count": None,
        "note": None,
        "tone": [],
        "bar": 0,
    }
    told = {
        key: (o["name"].removeprefix(name), o["ring"], o["filled"], o["count"], o["note"], o["bar"])
        for key, o in out.items()
        if o["note"]
    }
    assert told == {
        "one": (", 1 downloading", ["is-on"], 42, None, ", 42%", 1),
        "many": (", 4 downloading", ["is-on"], 40, None, ", 40% · 4", 1),
        "mixed": (", 1 ready to watch", ["is-on"], 57, "1", ", 57% · 4", 1),
        "ready": (", 1 ready to watch", ["is-on", "is-ready"], 100, "1", ", 1 ready", 0),
        "waiting": (
            ", 3 waiting to download",
            ["is-on", "is-waiting"],
            6,
            None,
            ", 3 waiting",
            0,
        ),
        # What it last was is not told as what it is.
        "lost": (", downloads not updating", ["is-on", "is-lost"], 57, None, ", Not updating", 0),
        "found": (", 4 downloading", ["is-on"], 40, None, ", 40% · 4", 1),
    }
    assert out["lost"]["tone"] == ["is-lost"]
    assert [out[key]["tone"] for key in ("mixed", "ready", "waiting")] == [
        [],
        ["is-ready"],
        ["is-waiting"],
    ]
    # nothing is left once the last one has gone
    over = out["over"]
    assert (over["name"], over["count"], over["note"]) == (name, None, None)
    assert "is-on" not in over["ring"]


def test_where_nothing_tells_of_downloads_the_menu_works_without_them() -> None:
    assert _run("noDownloads") == {"open": True, "name": "Profile menu, robin"}


def test_a_keyboard_is_told_from_a_toolbar_and_from_a_zoomed_page() -> None:
    assert _run("keyboard") == {
        "whole": False,
        "toolbar": False,
        "keyboard": True,
        "zoomed": False,
        "zoomedTyping": True,
        "away": False,
    }


def test_the_second_tap_of_a_double_tap_on_the_picture_presses_nothing_in_the_sheet() -> None:
    assert _run("doubleTap") == {
        "quick": True,
        "slow": False,
        "key": False,
        "desktop": False,
        "open": True,
    }


def test_a_ring_that_comes_back_does_not_sweep_round_from_where_it_last_stood() -> None:
    assert _run("ringComesBack") == {"eased": [False, True, False, True], "left": False}


def test_no_row_of_the_phone_sheet_lies_on_the_avatars_spot() -> None:
    sheet = (
        NAV_CSS.read_text()
        .split("/* Phones: a bottom sheet", 1)[1]
        .split("@media (prefers-reduced-motion", 1)[0]
    )
    # the sheet stops above where the avatar stands, so Log out is never under it
    menu = re.search(r"\.profile-menu \{[^}]*\}", sheet).group(0)  # type: ignore[union-attr]
    assert "padding: 0 8px max(8px, calc(var(--foot) - var(--foot-gap) + 8px))" in menu
    panel = re.search(r"\.profile-panel \{[^}]*\}", sheet).group(0)  # type: ignore[union-attr]
    assert "100dvh - var(--foot) + var(--foot-gap)" in panel


def test_the_handle_drags_the_sheet_shut_or_lets_it_spring_back() -> None:
    out = _run("drag")
    assert out["far"]["follow"] == ["translateY(30px)", "translateY(120px)"]
    assert (out["far"]["open"], out["far"]["transform"]) == (False, "translateY(100%)")
    assert (out["short"]["open"], out["short"]["transform"]) == (True, "")
    assert (out["upward"]["open"], out["upward"]["follow"]) == (True, ["translateY(0px)"])
    assert out["flick"]["open"] is False
    # reduced motion shuts it without the slide
    assert (out["still"]["open"], out["still"]["transform"]) == (False, "")
    # the next opening starts from rest
    assert out["far"]["reopened"] == {"open": True, "transform": ""}
