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
    assert out["closed"] == {"open": False, "expanded": "false", "focused": "desktop"}


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
    assert out["focused"] == "desktop"
    # the page's own Escape, such as closing a title, must not act as well
    assert out["escStopped"] is True


def test_arrow_up_on_the_avatar_opens_the_menu_at_its_last_item() -> None:
    assert _run("upOpensAtTheEnd") == {"focused": "logout"}


def test_the_space_bar_follows_a_menu_link_instead_of_scrolling_the_page() -> None:
    assert _run("space") == {"clicks": 1, "prevented": True}


def test_tab_closes_the_menu_and_goes_on_from_the_avatar() -> None:
    assert _run("tab") == {"open": False, "focused": "desktop", "prevented": False}


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
        "inert": ["top", "phone-top", "main", "already-inert"],
        "scrolls": False,
        "focused": "account",
    }
    assert out["freed"] == {
        "open": False,
        "inert": ["profile-menu", "already-inert"],
        "scrolls": True,
        "focused": "phone",
    }


def test_a_dialog_over_the_phone_sheet_keeps_the_page_out_of_reach_as_the_sheet_closes() -> None:
    assert _run("dialogOverSheet") == {
        "over": {
            "open": False,
            "inert": ["top", "phone-top", "profile-menu", "main", "already-inert"],
        },
        "freed": ["already-inert"],
    }


def test_the_phone_sheet_has_a_button_that_closes_it() -> None:
    assert _run("closeButton") == {
        "open": False,
        "inert": ["profile-menu", "already-inert"],
        "scrolls": True,
        "focused": "phone",
    }


def test_giving_the_focus_back_to_a_button_in_the_bar_never_scrolls_the_page() -> None:
    assert _run("focusBack") == dict.fromkeys(
        ("escape", "tab", "close", "button", "resize"), [True]
    )


def test_outgrowing_the_phone_layout_closes_the_sheet_and_moves_to_the_other_avatar() -> None:
    assert _run("resize") == {
        "open": False,
        "inert": ["profile-menu", "already-inert"],
        "scrolls": True,
        "focused": "desktop",
    }


def test_a_closed_menu_is_inert_while_it_fades_and_reopening_undoes_it_at_once() -> None:
    assert _run("fade") == {
        "fresh": False,
        "opened": False,
        "closed": {"inert": True, "focused": "desktop"},
        "reopened": {"inert": False, "focused": "account"},
        "sheet": {"inert": True, "focused": "phone"},
    }


def test_a_changed_avatar_takes_the_old_one_s_place_once_loaded_and_the_last_change_wins() -> None:
    assert _run("avatar") == {
        "loading": [None, None, None],
        "stale": [None, None, None],
        "shown": ["/new", "/new", "/new"],
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
