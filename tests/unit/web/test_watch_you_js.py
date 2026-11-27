"""The pane over Watch that holds Profile, History and Settings (watch/you.js), run under node
with the real address.js, modal-a11y.js and ui-feedback.js against a fake history."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_you_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_back_closes_a_deep_linked_pane_and_stays_on_watch() -> None:
    out = _run("deepLinkBack")
    assert out["opened"] == {
        "entries": ["/", "/?you=profile"],
        "shown": True,
        "title": "Profile",
        "focus": "panel",
    }
    assert (out["at"], out["shown"], out["hidden"]) == ("/", False, True)
    assert out["focus"] == "btn"
    assert out["told"] == ["shown profile profile", "hidden profile profile"]


def test_back_while_dirty_asks_with_keep_editing_focused_and_stays_until_discard_repeats_it() -> (
    None
):
    out = _run("backWhileDirty")
    assert out["asked"] == {
        "at": "/?you=profile/email",
        "title": "Email",
        "q": "Discard what you typed?",
        "focus": "Keep editing",
    }
    assert out["kept"]["at"] == "/?you=profile/email"
    assert out["kept"]["discarded"] == 0
    assert out["discarded"] == {"at": "/?you=profile", "title": "Profile", "discarded": 1}


def test_every_way_out_of_a_dirty_view_asks_first_and_moves_nothing() -> None:
    out = _run("everyWayOut")
    for way in ("close", "veil", "escape", "up", "step", "link", "pull", "back"):
        assert out[way] == [True, "/?you=profile/email"], way
    assert out["unload"] == [True, ""]
    assert out["clean"] is False
    assert out["assigned"] == []


def test_opening_focuses_the_panel_holds_the_page_and_tab_wraps_inside_it() -> None:
    out = _run("tabWraps")
    assert out["opened"] == "panel"
    assert (out["after"], out["back"]) == ("x", "last")
    assert out["page"] == [True, "true"]


def test_an_old_settings_link_opens_its_section_and_closing_gives_the_focus_back() -> None:
    out = _run("focusReturns")
    assert out["opened"] == ["/?you=settings/general", "Settings", ["/settings?pane=1"]]
    # Escape on a section of a narrow pane goes back to the list first
    assert out["list"] == ["/?you=settings", "Settings"]
    assert out["fromLink"] == ["/", "open-settings"]
    # opened by its address, the pane gives the focus to the profile button
    assert out["deep"] == ["/", "btn"]


def test_old_hashes_become_the_panes_places_and_unknown_places_are_profile() -> None:
    assert _run("oldHashes") == {
        "section": "/?you=settings/libraries",
        "card": "/?you=settings/integrations/openai",
        "plex": "/?you=profile/plex",
        "unknown": "/?you=profile&h=q%3Dx",
        "history": "/?you=history&h=action%3Dplays",
        "viewer": "/?q=dune",
    }


def test_a_redirect_to_login_is_followed_with_the_pane_as_next_and_its_hash_kept() -> None:
    assert _run("loginRedirect") == {
        "plain": ["/login?next=%2F%3Fyou%3Dsettings%2Flibraries"],
        "hash": ["/login?next=%2F%3Fyou%3Dsettings%2Flibraries%26h%3Dq%253Dx#title=movie-1"],
    }


def test_a_group_that_did_not_load_says_so_shows_profile_and_is_asked_for_again() -> None:
    out = _run("didNotLoad")
    assert out["first"] == ["/?you=profile", "Profile", ["That did not load. Try again."]]
    assert out["tries"] == 2


def test_a_viewer_at_settings_gets_no_pane_and_nothing_is_fetched() -> None:
    assert _run("viewerSettings") == {"at": "/", "fetched": [], "shown": False, "entries": 1}


def test_each_group_and_each_asset_loads_once_styles_then_scripts_in_order() -> None:
    out = _run("assetsOnce")
    assert out["fetched"] == ["/account?pane=1", "/history?pane=1&action=plays"]
    assert out["assets"] == [
        "/static/css/password.css?v=1",
        "/static/js/reauth.js?v=1",
        "/static/js/account.js?v=1",
        "/static/js/history.js?v=1",
    ]
    assert out["loaded"] == [
        "/static/js/reauth.js?v=1",
        "/static/js/account.js?v=1",
        "/static/js/history.js?v=1",
    ]


def test_with_reduced_motion_the_pane_is_put_away_after_its_fast_fade() -> None:
    out = _run("hideTimer")
    assert 260 in out["full"]
    assert 160 in out["calm"]
    assert 260 not in out["calm"]


def test_history_s_filters_go_in_h_with_only_its_own_keys() -> None:
    out = _run("historyAddress")
    assert out["pushed"] == ["/?you=history&h=q%3Ddune%2B2%26action%3Dplays", 3, "dune 2"]
    assert out["replaced"] == ["/?you=history&h=page%3D2", 3]


def test_a_form_no_script_took_is_kept_from_posting_to_watch() -> None:
    assert _run("submits") == {"loose": True, "logout": False}


def test_a_place_without_a_view_of_its_own_shows_the_nearest_and_says_where_it_is() -> None:
    out = _run("shownAndScroller")
    assert out["shown"] == [True, False]
    assert out["scroller"] == "settings"
    assert out["told"] == ["shown settings settings/integrations/openai"]
    assert out["wide"] is True
    assert out["up"] == [False, "Settings"]


def test_the_pane_opened_over_a_title_closes_it() -> None:
    assert _run("overATitle") == "/?you=profile"


def test_a_narrow_pane_titles_an_open_settings_section_by_its_own_name_and_follows_the_width() -> (
    None
):
    out = _run("sectionTitle")
    assert out["narrow"] == ["Integrations", False, "Settings"]
    # beside the list on a wide pane, the pane is Settings and Up goes to Profile
    assert out["widened"] == ["Settings", False, "Profile"]
    assert out["narrowed"] == ["Integrations", False, "Settings"]
    assert out["missing"] == ["Settings", False, "Settings"]


def test_a_drag_from_the_panel_that_ends_on_the_veil_keeps_the_pane_and_a_press_on_it_closes() -> (
    None
):
    out = _run("veilDrag")
    assert out["dragged"] == [True, "/?you=profile"]
    assert out["pressed"] == [False, "/"]


def test_with_nothing_unsaved_up_and_the_close_button_still_work() -> None:
    out = _run("stillCloses")
    assert out["ups"] == [False, False]
    assert out["upped"] == ["/?you=profile", "Profile", False]
    assert out["closed"] == ["/", False, False, False]


def test_forward_to_a_group_still_loading_shows_it_once_when_it_comes() -> None:
    out = _run("forwardWhileLoading")
    assert out["waiting"] == ["/?you=history", True, True]
    assert out["came"] == ["History", False]
    assert out["told"] == ["hidden history history", "shown history history"]
    assert out["fetched"] == ["/history?pane=1"]


def test_a_sheet_pulled_shut_while_its_group_loads_stays_shut_when_it_comes() -> None:
    out = _run("pullWhileLoading")
    assert out["pulled"] == ["/", False]
    assert out["after"] == ["/", False, True, False]
    assert out["told"] == ["hidden history history"]


def test_a_fragment_s_styles_and_scripts_load_only_from_the_app_s_own_static_files() -> None:
    out = _run("foreignAssets")
    assert out["assets"] == ["/static/css/ok.css", "/static/js/ok.js"]
    # a script it may not load leaves the group not loaded
    assert out["toasts"] == ["That did not load. Try again."]


def test_history_is_fetched_with_only_its_own_keys_from_the_address() -> None:
    assert _run("historyKeys") == ["/history?pane=1&action=plays"]


def test_profile_is_fetched_ahead_only_on_intent_and_history_and_settings_once_the_hub_shows() -> (
    None
):
    out = _run("prefetch")
    admin, viewer = out["admin"], out["viewer"]
    assert admin["before"] == []
    # an idle page asks for nothing: only the pointer, a press or the focus on the button does
    assert admin["idle"] == []
    assert admin["opened"] == ["Profile", 1]
    assert admin["hub"] == ["/account?pane=1", "/history?pane=1", "/settings?pane=1"]
    # History asked for with other filters than it was fetched ahead with is fetched afresh
    assert (admin["filtered"], admin["title"]) == (["/history?pane=1&action=plays"], "History")
    assert viewer["hub"] == ["/account?pane=1", "/history?pane=1"]
    assert admin["toasts"] == viewer["toasts"] == []


def test_a_fetch_ahead_that_fails_says_nothing_and_is_asked_for_again_on_opening() -> None:
    out = _run("prefetchFails")
    assert out["quiet"] == [[], False, "/"]
    assert out["opened"] == ["Profile", ["/account?pane=1", "/account?pane=1"], []]


def test_the_pointer_on_the_profile_button_fetches_profile_ahead_once() -> None:
    out = _run("prefetchOnButton")
    assert out["hovered"] == ["/account?pane=1"]
    assert out["pressed"] == ["/account?pane=1"]


def test_a_step_to_a_group_on_its_way_slides_the_old_view_out_and_puts_the_focus_on_the_title() -> (
    None
):
    out = _run("stepWhileLoading")
    assert out["waiting"] == {
        "on": [],
        "loading": True,
        "focus": "title",
        "hubDir": -1,
        "hubInert": True,
    }
    assert (out["came"], out["title"]) == (["history"], "History")


def test_a_pull_down_on_the_content_at_its_top_closes_the_sheet_and_a_scroll_does_not() -> None:
    out = _run("pullFromContent")
    assert out["top"]["kept"] == [True, True, True]
    assert out["top"]["moved"].startswith("translateY(")
    assert out["top"]["at"] == "/"
    for way in ("scrolled", "up", "side", "inner", "innerStart"):
        assert out[way]["kept"] == [False] * len(out[way]["kept"]), way
        assert out[way]["at"] == "/?you=profile", way


def test_a_sheet_pulled_up_gives_less_the_further_it_is_pulled_never_past_36px_and_comes_back() -> (
    None
):
    out = _run("rubberBand")
    gave = [-float(t.removeprefix("translateY(").removesuffix("px)")) for t in out["up"]]
    assert 3 <= gave[0] <= 10
    assert gave == sorted(gave) and len(set(gave)) == 4
    # pulled 10, 100, 300 then 3000 px: each further pixel gives less than the last
    assert (gave[1] - gave[0]) / 90 > (gave[2] - gave[1]) / 200 > (gave[3] - gave[2]) / 2700
    assert gave[3] <= 36 and gave[3] > 30
    assert out["after"] == ["", "/?you=profile"]


def test_a_step_moves_the_old_title_out_and_the_new_in_and_with_reduced_motion_only_fades() -> None:
    out = _run("titleMoves")
    full = out["full"]["step"]
    assert full["title"] == "Email"
    assert full["ghost"] == ["Profile", "true"]
    assert full["out"] == [[[1, "none"], [0, "translateX(-12px)"]]]
    assert full["in"] == [[[0, "translateX(12px)"], [1, "none"]]]
    assert full["label"] == [[[0, "translateX(12px)"], [1, "none"]]]
    assert out["full"]["left"] == 0
    # back up, both move the other way
    title, gone, came = out["full"]["back"]
    assert title == "Profile"
    assert gone == [[[1, "none"], [0, "translateX(12px)"]]]
    assert came == [[[0, "translateX(-12px)"], [1, "none"]]]
    calm = out["calm"]["step"]
    assert calm["out"] == [[[1, "none"], [0, "none"]]]
    assert calm["in"] == [[[0, "none"], [1, "none"]]]
    assert calm["timing"]["in"][0][0] >= calm["timing"]["out"][0][1] - 30


def test_the_old_title_is_gone_before_the_new_one_is_legible_and_the_new_moves_with_the_view() -> (
    None
):
    out = _run("titleMoves")
    timing = out["full"]["step"]["timing"]
    (gone_delay, gone_ms, _), (came_delay, came_ms, came_ease) = timing["out"][0], timing["in"][0]
    assert gone_delay == 0 and gone_ms <= 100
    # the new title starts once the old is mostly faded, and ends with the step
    assert 50 <= came_delay <= gone_ms
    assert came_delay + came_ms == 250
    tokens = _run("widthSteps")["full"]["tokens"]
    assert came_ease == tokens["--ease-push"]


def test_stepping_between_a_narrow_and_a_wide_view_moves_the_panel_s_edge_and_never_its_width() -> (
    None
):
    full = _run("widthSteps")["full"]
    push = full["tokens"]["--ease-push"]
    back, into = full["back"], full["into"]
    # back to the narrow hub: the box is held at the widest width, 1100 px, while the frame's left
    # edge moves 440 px to the right, from History's 880 px to the hub's 440
    assert back["held"] and back["frames"] == 1
    assert back["edge"] == [["translateX(220px)", "translateX(660px)"]]
    assert back["fill"] == [["scaleX(0.8)", "scaleX(0.4)"]]
    # the hub comes in with the edge, its own left on it from first frame to last, and opaque
    assert back["hub"] == [["translateX(-440px)", "translateX(0px)"]]
    assert back["hubShown"] == [[1, 1]]
    assert back["ease"] == [push]
    # Back and the title keep to the frame: Back on its edge, the title in its middle
    assert back["up"] == [["220px", "660px"]]
    assert back["title"] == [["110px", "330px"]] and back["old"] == [["110px", "330px"]]
    # once the step ends, the panel is its own width again and nothing is held
    assert back["after"] == [False, 0]
    # into the wide view, the other way
    assert into["edge"] == [["translateX(660px)", "translateX(220px)"]]
    assert into["fill"] == [["scaleX(0.4)", "scaleX(0.8)"]]
    assert into["hub"] == [["translateX(0px)", "translateX(-440px)"]]
    assert into["title"] == [["330px", "110px"]] and into["old"] == [["330px", "110px"]]
    assert into["after"] == [False, 0]
    assert full["at"] == "/?you=history&h=action%3Dplays"


def test_settings_opens_wider_than_history_and_the_edge_moves_all_the_way_from_the_hub() -> None:
    out = _run("settingsSteps")
    into, back = out["full"]["into"], out["full"]["back"]
    push = out["full"]["tokens"]["--ease-push"]
    # 440 px to 1100 px: the frame's edge runs the whole 660 px between them, on the push curve
    assert into["held"] and into["edge"] == [["translateX(660px)", "translateX(0px)"]]
    assert into["fill"] == [["scaleX(0.4)", "scaleX(1)"]]
    assert into["hub"] == [["translateX(0px)", "translateX(-660px)"]] and into["ease"] == [push]
    assert into["up"] == [["660px", "0px"]] and into["title"] == [["330px", "0px"]]
    assert back["edge"] == [["translateX(0px)", "translateX(660px)"]]
    assert back["hub"] == [["translateX(-660px)", "translateX(0px)"]]
    assert into["after"] == back["after"] == [False, 0]
    # with reduced motion the wide frame only fades over the narrow, nothing moves
    calm = out["calm"]["into"]
    assert (
        calm["fade"] == [[], [[0, 1]]]
        and all(a == b for a, b in calm["edge"])
        and calm["hub"] == []
    )


def test_a_group_that_comes_in_a_moment_is_pushed_over_the_view_on_show_not_after_a_wait() -> None:
    first = _run("widthSteps")["full"]["first"]
    # History was not yet in: it came within the moment, so the hub stayed to travel with the edge
    assert first["hub"] == [["translateX(0px)", "translateX(-440px)"]]
    assert first["edge"] == [["translateX(660px)", "translateX(220px)"]]


def test_with_reduced_motion_the_wide_frame_fades_over_the_narrow_and_nothing_slides() -> None:
    calm = _run("widthSteps")["calm"]
    for way, fade in (("back", [1, 0]), ("into", [0, 1])):
        step = calm[way]
        assert step["held"] and step["frames"] == 2, way
        # the narrow frame stands, the wide one over it fades
        assert step["fade"] == [[], [fade]], way
        assert all(a == b for a, b in step["edge"]), way
        assert step["hub"] == [], way
        assert step["after"] == [False, 0], way


def test_on_a_phone_the_sheet_keeps_its_width_and_no_frame_is_drawn() -> None:
    phone = _run("widthSteps")["phone"]
    for way in ("back", "into"):
        assert (phone[way]["held"], phone[way]["frames"], phone[way]["hub"]) == (False, 0, []), way


def test_with_reduced_motion_a_back_that_fades_out_stays_at_its_start_and_one_that_fades_in_at_its_end() -> (
    None
):
    calm = _run("widthSteps")["calm"]
    # back to the hub, which has no Back: it fades where it was, not in the middle of the bar
    assert calm["back"]["up"] == [["220px", "220px"]]
    # into the wide view Back comes in at its end position
    assert calm["into"]["up"] == [["220px", "220px"]]


def test_a_profile_view_a_step_below_another_goes_back_to_that_one() -> None:
    out = _run("nestedProfile")
    assert out["deep"] == ["/?you=profile/subtitles/language", "Subtitle language", False]
    assert out["escaped"] == ["/?you=profile/subtitles", "Subtitles"]
    assert out["upped"] == ["/?you=profile/subtitles", "Subtitles"]
    assert out["hub"] == ["/?you=profile", "Profile", True]
