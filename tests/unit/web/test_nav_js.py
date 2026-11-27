"""nav.js run under node against a fake DOM: the profile menu and its phone sheet."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "nav_harness.js"
NAV_CSS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_nav.css"


def _run(scenario: str, **opts) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario, json.dumps(opts)],
        capture_output=True,
        text=True,
        check=False,
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


def test_the_ring_and_the_count_say_how_what_is_downloading_stands_with_no_note_in_the_menu() -> (
    None
):
    out = _run("downloads")
    name = "Profile menu, robin"
    assert out["none"] == {"name": name, "ring": [], "count": None}
    told = {
        key: (o["name"].removeprefix(name), o["ring"], o["filled"], o["count"])
        for key, o in out.items()
        if key not in ("none", "over")
    }
    assert told == {
        "one": (", 1 downloading", ["is-on"], 42, None),
        "many": (", 4 in progress", ["is-on"], 40, None),
        "mixed": (", 1 ready to watch", ["is-on"], 57, "1"),
        "ready": (", 1 ready to watch", ["is-on", "is-ready"], 100, "1"),
        "waiting": (", 3 waiting to download", ["is-on", "is-waiting"], 6, None),
        # What it last was is not told as what it is.
        "lost": (", downloads not updating", ["is-on", "is-lost"], 57, None),
        "found": (", 4 in progress", ["is-on"], 40, None),
        # once seen, what is ready is told on its own and not counted as in progress
        "seenMixed": (", 3 in progress, 1 ready to watch", ["is-on"], 57, None),
        "seenDownloading": (", 1 downloading, 2 ready to watch", ["is-on"], 30, None),
        "seenWaiting": (
            ", 1 waiting to download, 1 ready to watch",
            ["is-on", "is-ready"],
            6,
            None,
        ),
    }
    # nothing is left once the last one has gone
    over = out["over"]
    assert (over["name"], over["count"]) == (name, None)
    assert "is-on" not in over["ring"]


def test_the_label_counts_the_ready_titles_downloading_lists_not_all_the_summary_counts() -> None:
    out = _run("cappedReady")
    assert out["mixed"].endswith(", 1 downloading, 2 ready to watch")
    assert out["ready"].endswith(", 2 ready to watch")


def test_on_watch_the_profile_button_opens_the_pane_and_never_the_menu() -> None:
    assert _run("onWatch") == {
        "went": ["profile"],
        "open": False,
        "arrowed": False,
        "popup": "dialog",
        "controls": "you",
    }


def test_where_nothing_tells_of_downloads_the_menu_works_without_them() -> None:
    assert _run("noDownloads") == {"open": True, "name": "Profile menu, robin"}


def test_the_brand_takes_its_island_once_the_page_has_moved_under_the_bar() -> None:
    assert _run("scrolled") == {"rest": False, "down": True, "nudged": False, "back": False}


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


def test_the_phone_sheet_stands_clear_of_the_home_bar_and_the_top_of_the_screen() -> None:
    sheet = (
        NAV_CSS.read_text()
        .split("/* Phones: a bottom sheet", 1)[1]
        .split("@media (prefers-reduced-motion", 1)[0]
    )
    menu = re.search(r"\.profile-menu \{[^}]*\}", sheet).group(0)  # type: ignore[union-attr]
    assert "padding: 0 8px max(8px, env(safe-area-inset-bottom, 0px))" in menu
    panel = re.search(r"\.profile-panel \{[^}]*\}", sheet).group(0)  # type: ignore[union-attr]
    assert "max-height: calc(100dvh - 24px)" in panel


def test_the_handle_drags_the_sheet_shut_or_lets_it_spring_back() -> None:
    out = _run("drag")
    assert out["far"]["follow"] == ["translateY(30px)", "translateY(120px)"]
    assert (out["far"]["open"], out["far"]["transform"]) == (False, "translateY(100%)")
    assert (out["short"]["open"], out["short"]["transform"]) == (True, "")
    # slow and under the distance
    assert (out["slow"]["open"], out["slow"]["transform"]) == (True, "")
    assert (out["upward"]["open"], out["upward"]["follow"]) == (True, ["translateY(0px)"])
    # reduced motion shuts it without the slide
    assert (out["still"]["open"], out["still"]["transform"]) == (False, "")


def test_a_short_quick_flick_shuts_the_sheet_even_after_a_pause() -> None:
    assert _run("drag")["flick"]["open"] is False


def test_each_opening_after_a_drag_starts_from_rest_unseen() -> None:
    out = _run("drag")["far"]["reopened"]
    assert out["open"] is True
    # put back with no transition, then the transition returns, and only then does it open
    assert out["log"] == [
        "transition=none",
        "transform=",
        "layout",
        "transition=",
        "open",
    ]


def test_the_handle_strip_is_as_tall_as_a_thumb() -> None:
    sheet = NAV_CSS.read_text().split("/* Phones: a bottom sheet", 1)[1]
    strip = re.search(r"\.profile-handle \{[^}]*\}", sheet).group(0)  # type: ignore[union-attr]
    assert "height: 44px" in strip


def test_a_flick_back_up_keeps_the_sheet_open_however_far_it_was_pulled() -> None:
    out = _run("drag")
    assert (out["back"]["open"], out["back"]["transform"]) == (True, "")
    # a slow release past the distance still shuts it
    assert out["slowFar"]["open"] is False


def test_a_drag_that_loses_its_pointer_springs_back() -> None:
    out = _run("drag")["lost"]
    assert (out["open"], out["transform"], out["after"]) == (True, "", True)


def test_the_page_is_marked_while_the_focus_is_in_the_bar_and_only_then() -> None:
    # in, moved within, left for the page, settled there, left for nothing, in Downloading's head
    assert _run("barFocus") == [True, True, False, False, False, True]


def test_the_ring_is_a_link_of_its_own_beside_the_face_while_something_downloads() -> None:
    out = _run("ringLink")
    sibling = {"tag": "a", "inButton": False, "sibling": True, "href": "/?dl=1"}
    # off, there is only the profile button
    assert out["off"]["hidden"] is True
    assert {**out["on"], "label": None, "hidden": None} == {
        **sibling,
        "label": None,
        "hidden": None,
    }
    assert out["on"]["hidden"] is False
    assert out["on"]["label"] == "Downloading, 89%"
    assert out["waiting"] == "2 waiting to download"
    assert out["ready"] == "2 ready to watch"
    assert out["lost"] == "Downloads not updating"
    assert out["focused"] is True
    assert out["gone"] is True


def test_a_tap_on_the_face_opens_the_pane_and_a_tap_on_the_ring_leaves_it_to_the_link() -> None:
    out = _run("ringLink")
    assert out["face"] == ["profile"]
    assert out["menuAfterFace"] is False
    # the link's own press is not taken by the menu or the pane
    assert out["ring"] == []
    assert out["ringPrevented"] is False


def test_the_ring_link_covers_the_ring_and_a_thumb_s_width_but_leaves_the_face_to_its_button() -> (
    None
):
    css = NAV_CSS.read_text()
    rule = re.search(r"\.profile-dl\s*\{([^}]*)\}", css).group(1)
    assert "width: 44px" in rule and "height: 44px" in rule
    # a ring-shaped area: the picture inside it is still the profile button's
    assert "clip-path: path(evenodd" in rule


def test_the_number_counts_only_what_is_ready_and_not_yet_seen() -> None:
    out = _run("unseen")
    # two of three are new; one only added to the library and one downloading are not counted
    assert out["first"]["count"] == "2"
    assert out["first"]["badge"] == "2 ready to watch"
    assert out["first"]["ring"] == "2 ready to watch"
    assert out["first"]["name"].endswith(", 2 ready to watch")
    assert out["first"]["menu"] == "2 ready to watch"
    # what is no longer ready is forgotten
    assert out["pruned"] == ["radarr::1"]


def test_opening_a_title_or_seeing_it_in_downloading_takes_it_off_the_count() -> None:
    out = _run("unseen")
    assert out["opened"]["count"] == "1"
    shown = out["shown"]
    assert (shown["count"], shown["menu"]) == (None, None)
    # with nothing new, the ring tells what still downloads
    assert shown["ring"] == "Downloading, 50%"
    assert out["kept"] == ["radarr::1", "radarr::2", "radarr::3"]
    assert out["newTab"] is False


def test_what_one_person_has_seen_leaves_the_count_of_another_on_the_same_browser() -> None:
    assert _run("unseen")["otherPerson"] == "3"


def test_ready_titles_the_strip_does_not_list_are_never_counted() -> None:
    out = _run("unlisted")
    assert out["count"] is None
    assert out["name"] == "Profile menu, robin, 20 ready to watch"


def test_a_browser_that_refuses_its_storage_still_counts_and_forgets_what_was_seen() -> None:
    assert _run("noStorage") == {"first": "2", "seen": "1"}


def test_a_tap_on_the_number_follows_the_ring_s_link_and_opens_no_menu() -> None:
    assert _run("badgeTap") == {"followed": ["/?dl=1"], "went": [], "open": False}


def test_the_menu_line_of_what_is_ready_is_reached_only_while_it_shows() -> None:
    assert _run("readyKeys") == {"hidden": "account", "shown": "profile-ready", "href": "/?dl=1"}


def test_the_number_scales_out_on_the_exit_curve_and_only_fades_with_reduced_motion() -> None:
    css = NAV_CSS.read_text()
    rest = re.search(r"\n\.profile-count\s*\{([^}]*)\}", css).group(1)
    assert "scale(0)" in rest
    assert "transform var(--dur) var(--ease-exit)" in rest
    calm = css[css.index("@media (prefers-reduced-motion: reduce)") :]
    calm = calm[: calm.index("\n}\n")]
    # the same whether it comes or goes
    rule = re.search(r"\.profile-count,\s*\.profile-count\.is-on\s*\{([^}]*)\}", calm).group(1)
    assert "transform: none" in rule and "opacity var(--dur)" in rule


SIGNED_OUT = "/login?out=1"


def test_log_out_posts_at_once_and_opens_sign_in_once_answered_and_played() -> None:
    out = _run("logOut")
    assert out["prevented"] is True
    assert out["sent"] == [
        [
            "http://app.test/api/auth/logout",
            {
                "method": "POST",
                "credentials": "same-origin",
                "redirect": "manual",
                "keepalive": True,
            },
        ]
    ]
    assert out["leaving"] == ["is-leaving"]
    # a second press while leaving sends nothing more
    assert out["sentTwice"] == 1
    assert out["leaveMs"] == 980
    # answered first, it waits for the leaving to play out
    assert out["steps"] == [["answer", [], 0], ["played", [SIGNED_OUT], 0]]
    assert out["atLatest"] == [[SIGNED_OUT], 0]


def test_log_out_played_out_first_waits_for_the_answer() -> None:
    out = _run("logOut", playedFirst=True)
    assert out["steps"] == [["played", [], 0], ["answer", [SIGNED_OUT], 0]]


def test_the_menu_folds_into_the_avatar_or_only_fades_where_the_avatar_is_out_of_sight() -> None:
    # 256 px wide to 32: an eighth, its middle on the avatar's
    assert _run("logOut")["fold"] == ["224px", "-59px", "0.125"]
    off = _run("logOut", avatarAt=-80)
    assert off["leaving"] == ["is-leaving-flat", "is-leaving"]
    assert off["fold"] == [None, None, None]


def test_a_session_already_ended_goes_to_sign_in_too() -> None:
    out = _run("logOut", answer={"type": "basic", "status": 401})
    assert out["steps"][-1] == ["played", [SIGNED_OUT], 0]


def test_log_out_that_cannot_reach_the_server_or_is_refused_posts_the_form_as_before() -> None:
    for answer in ("offline", {"type": "basic", "status": 500}):
        out = _run("logOut", answer=answer)
        assert out["steps"] == [["answer", [], 1], ["played", [], 1]]
        # the latest-by timer does not open sign-in over the form's own post
        assert out["atLatest"] == [[], 1]


def test_log_out_never_answered_goes_home_after_two_seconds_and_does_not_say_signed_out() -> None:
    out = _run("logOut", answer="never")
    assert out["steps"] == [["answer", [], 0], ["played", [], 0]]
    assert out["atLatest"] == [["/"], 0]


def test_log_out_under_reduced_motion_waits_only_for_its_fade() -> None:
    out = _run("logOut", reduced=True)
    assert out["leaveMs"] == 150
    assert out["steps"][-1] == ["played", [SIGNED_OUT], 0]


def test_a_page_back_from_the_history_after_log_out_is_whole_again() -> None:
    out = _run("pageshow")
    assert out["fresh"] == ["is-leaving", "is-leaving-flat"]
    assert out["restored"] == []


def test_a_page_with_no_menu_back_from_the_history_after_log_out_is_whole_again() -> None:
    out = _run("pageshow", menuless=True)
    assert out["fresh"] == ["is-leaving", "is-leaving-flat"]
    assert out["restored"] == []
