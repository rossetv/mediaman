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


def test_a_redirect_to_login_is_followed_with_the_pane_as_next() -> None:
    assert _run("loginRedirect") == ["/login?next=%2F%3Fyou%3Dsettings%2Flibraries"]


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
        "/s/password.css?v=1",
        "/j/reauth.js?v=1",
        "/j/account.js?v=1",
        "/j/history.js?v=1",
    ]
    assert out["loaded"] == ["/j/reauth.js?v=1", "/j/account.js?v=1", "/j/history.js?v=1"]


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
