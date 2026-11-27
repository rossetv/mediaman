"""Downloading on Watch, grown out of its row at /?dl=1: the real scripts under node against a fake
DOM, with a fake history and a clock that is stepped."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.helpers.node_batch import run_node

HARNESS = Path(__file__).parent / "js" / "watch_dlgrid_harness.js"


@pytest.fixture(scope="module")
def out() -> dict:
    result = run_node(HARNESS, timeout=60)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    broken = {name: got["error"] for name, got in data.items() if "error" in got}
    assert not broken, broken
    return data


def _y(transform: str) -> float:
    """The pixels a translateY moves by."""
    return float(re.fullmatch(r"translateY\((-?[\d.]+)px\)", transform)[1])


def test_see_all_in_the_row_pushes_the_address_and_grows_the_row_in_place(out) -> None:
    run = out["opensFromTheRow"]
    assert run["drawn"] == {"href": "/?dl=1", "opens": True}
    assert run["pushed"] == "/?dl=1"
    # the rows stay under it while it grows, and are gone once it has
    assert run["growing"] == {"open": True, "rowsStill": True}
    assert run["opened"] == {
        "open": True,
        "rowsAway": True,
        "on": "watch-dl-title",
        "at": "/?dl=1",
        "entries": 2,
    }


def test_its_head_takes_its_glass_under_the_bar_and_stays_put_as_the_grid_folds(out) -> None:
    got = out["stuckHead"]
    assert got["glass"] is True
    assert (got["still"], got["under"], got["back"]) == (False, True, False)
    # Fixed to fold, the grid takes its head along unless it is held where it stood.
    assert got["folding"] == {"stuck": False, "at": 72}
    assert got["folded"] == {"stuck": False, "top": ""}


def test_the_count_beside_the_row_s_name_opens_it_too(out) -> None:
    assert out["countOpensToo"]["at"] == "/?dl=1"


def test_escape_folds_it_back_into_the_row_and_the_focus_returns_to_what_opened_it(out) -> None:
    run = out["opensFromTheRow"]
    assert run["folded"] == {"at": "/", "open": False, "rowsAway": False, "on": "row-open"}


def test_it_folds_no_slower_than_it_grows(out) -> None:
    run = out["opensFromTheRow"]
    # a fold under 100 ms reads as a snap, not as motion
    assert 100 <= run["fold"] <= run["grow"]


def test_the_rows_above_go_up_and_those_below_go_down_as_the_row_s_cards_grow(out) -> None:
    run = out["grows"]
    above = [_y(t) for t in run["above"]]
    below = [_y(t) for t in run["below"]]
    assert len(above) == 2 and all(y < 0 for y in above)
    assert len(below) == 1 and all(y > 0 for y in below)
    # cut to the row card's size at first, so no empty band shows while it grows
    assert (run["card"]["from"], run["card"]["to"]) == (True, "none")
    assert run["card"]["ms"] > 0
    assert run["title"] == [True], "the row's name travels into the header"


def test_a_bar_scrolled_out_of_sight_comes_down_with_the_grow_and_goes_up_with_the_fold(
    out,
) -> None:
    run = out["barAndChevrons"]["away"]
    [grow] = run["grow"]["bar"]
    [fold] = run["fold"]["bar"]
    # held where it ends until the page is scrolled to match, so it never blinks away first
    assert grow["fill"] == fold["fill"] == "forwards"
    hidden, shown = grow["frames"]
    assert (hidden["opacity"], shown["opacity"]) == (0, 1)
    assert _y(hidden["transform"]) < _y(shown["transform"]) == run["y"] == 600, (
        "comes down to the top of the page"
    )
    assert fold["frames"] == [shown, hidden], "and goes back up the way it came"


def test_a_bar_partly_in_sight_slides_without_fading_and_one_that_stays_is_left_alone(
    out,
) -> None:
    near = out["barAndChevrons"]["near"]
    start, end = near["grow"]["bar"][0]["frames"]
    assert (start["opacity"], end["opacity"]) == (1, 1)
    assert _y(start["transform"]) < _y(end["transform"]) == near["y"] == 30, (
        "slides down to the top of the page"
    )
    assert out["barAndChevrons"]["sticky"]["grow"]["bar"] == []
    assert out["barAndChevrons"]["sticky"]["fold"]["bar"] == []


def test_the_row_s_chevrons_fade_with_the_row_as_it_grows_and_come_back_as_it_folds(
    out,
) -> None:
    run = out["barAndChevrons"]["away"]
    assert [a["frames"][-1]["opacity"] for a in run["grow"]["arrow"]] == [0]
    assert [a["frames"][-1]["opacity"] for a in run["fold"]["arrow"]] == [1]


def test_with_reduced_motion_it_only_fades_in_150_ms(out) -> None:
    run = out["reduced"]
    assert run["open"] == {"durations": [150], "keys": ["opacity"], "shown": True}
    assert run["fold"] == {"durations": [150], "keys": ["opacity"], "shown": False}


def test_the_chevron_and_back_fold_it_and_leave_the_page_on_watch(out) -> None:
    run = out["chevronAndBack"]
    assert run["chevron"] == {"at": "/", "open": False, "index": 0}
    assert run["back"] == {"at": "/", "open": False, "index": 0}


def test_a_deep_link_opens_it_at_once_and_back_lands_on_watch(out) -> None:
    run = out["deepLink"]
    assert run["at"] == {
        "entries": ["/", "/?dl=1"],
        "index": 1,
        "open": True,
        "rowsAway": True,
        "anims": 0,
        "on": "watch-dl-title",
    }
    assert run["drawn"] == 3
    assert run["back"] == {"url": "/", "index": 0, "open": False, "rowsAway": False}


def test_only_dl_1_opens_it_and_anything_else_is_taken_out_of_the_address(out) -> None:
    assert out["otherValuesAreTakenOut"] == {"at": "/?x=1", "open": False}


def test_an_open_grid_of_a_row_closes_before_downloading_opens(out) -> None:
    assert out["gridClosesFirst"] == {"log": ["close grid push"], "at": "/?dl=1", "open": True}


def test_downloads_are_grouped_needs_attention_first_with_their_counts(out) -> None:
    run = out["groups"]
    assert run["order"] == [
        ["Needs attention · 2", "Windward", "Tallow Hill"],
        ["Downloading · 3", "Ember Coast", "Honest Forger", "Sundial"],
        ["Looking for a copy · 3", "Quiet Engines", "Cobblestone", "Finchley"],
        ["Coming soon · 2", "Lanterns", "Orchard"],
    ]
    assert run["title"] == "Downloading"
    assert run["count"] == "8 in progress · 2 coming soon"


def test_queued_rows_that_sat_under_up_next_on_the_old_page_wait_their_turn_under_downloading(
    out,
) -> None:
    cards = out["groups"]["cards"]
    assert cards["Honest Forger"]["line"] == "Queued"
    assert cards["Honest Forger"]["say"] == "Waiting its turn · 3.2 GB"


def test_each_card_says_in_one_line_under_it_what_the_old_page_said_of_its_download(out) -> None:
    cards = out["groups"]["cards"]
    assert cards["Ember Coast"]["line"] == "43% · 5 min left"
    assert cards["Ember Coast"]["say"] == "4.1 GB of 9.6 GB"
    assert cards["Sundial"]["line"] == "Waiting to import"
    assert cards["Sundial"]["say"] == "2 GB of 2 GB · Post-processing…"
    assert cards["Quiet Engines"]["say"] == "Searched 11× · next attempt in ~24h"
    assert cards["Cobblestone"]["say"] == "Searched once · next attempt in 1m"
    assert cards["Finchley"]["say"] == "Finding another copy"


def test_a_line_cut_short_cuts_the_sizes_and_keeps_what_follows_them(out) -> None:
    cards = out["groups"]["cards"]
    assert cards["Sundial"]["kept"] == "· Post-processing…"
    assert cards["Ember Coast"]["kept"] is None


def test_an_admin_is_told_what_is_wrong_on_its_line_and_whole_with_what_comes_of_it_in_its_menu(
    out,
) -> None:
    run = out["groups"]
    windward = run["cards"]["Windward"]
    assert windward["line"] == "Needs attention"
    assert windward["say"] == "Import failed, path does not exist"
    details = run["menus"]["Windward"]["items"][0]
    assert details[0] == (
        "DetailsRadarr says: Import failed, path does not existFix it in Radarr: Activity → Queue"
    )
    assert details[4] == "true", "it is read, not pressed"
    retrying = run["menus"]["Tallow Hill"]["items"][0][0]
    assert retrying.endswith("mediaman will look for another copy")


def test_an_admin_is_given_the_link_into_radarr_only_to_a_web_address(out) -> None:
    menus = out["groups"]["menus"]
    link = ["Open in Radarr", "http://radarr.local/movie/1", "_blank", "noopener noreferrer"]
    assert menus["Windward"]["items"][1][:4] == link
    assert menus["Cobblestone"] is None, "javascript: is no link, and nothing else is in its menu"
    assert out["groups"]["cards"]["Ember Coast"]["controls"] == [], "nor while it downloads"


def test_abandon_is_last_in_the_menu_in_red_after_a_divider(out) -> None:
    items = out["groups"]["menus"]["Quiet Engines"]["items"]
    assert [i if i == "---" else [i[0], i[5]] for i in items] == [
        ["Open in Radarr", False],
        "---",
        ["Abandon search…", True],
    ]


def test_the_menu_is_named_by_its_title_and_escape_shuts_it_onto_its_button(out) -> None:
    menu = out["groups"]["menus"]["Quiet Engines"]
    assert menu["label"] == "More for Quiet Engines"
    assert (menu["back"], menu["shut"]) == (True, "false")


def test_every_menu_and_stop_tracking_button_is_named_by_its_title(out) -> None:
    cards = out["groups"]["cards"]
    assert cards["Quiet Engines"]["controls"] == [["more", "More for Quiet Engines"]]
    assert cards["Lanterns"]["controls"] == [["stop", "Stop tracking Lanterns"]]
    # what the abandon dialog reads off the menu's button, which opens the menu and not the dialog
    assert out["groups"]["trigger"] == [
        "radarr:Quiet Engines",
        "7",
        "movie",
        "Quiet Engines",
        "[]",
        None,
    ]
    assert out["groups"]["stop"] == ["1", "Stop tracking"]


def test_only_a_download_the_server_lets_go_of_has_abandon_in_its_menu(out) -> None:
    menus = out["groups"]["menus"]
    with_abandon = [
        t
        for t, m in menus.items()
        if m and any(i != "---" and i[0] == "Abandon search…" for i in m["items"])
    ]
    assert with_abandon == ["Quiet Engines"]


def test_a_viewer_is_never_drawn_a_menu_a_stop_tracking_or_a_reason_even_if_sent_one(
    out,
) -> None:
    run = out["viewer"]
    assert (run["triggers"], run["menus"], run["buttons"], run["menu"]) == (0, 0, 0, False)
    assert not run["modal"]
    assert run["links"] == 0, "no way out to Radarr or Sonarr"
    assert run["say"] == [
        "Won’t finish until an admin sorts it out",
        "mediaman will look for another copy",
        "Searched 11× · next attempt in ~24h",
    ]


def test_a_poll_patches_a_card_in_place_and_keeps_its_open_episodes_and_focus(out) -> None:
    after = out["patched"]["after"]
    assert after["same"] and after["on"]
    assert (after["expanded"], after["listShown"]) == ("true", True)
    assert after["rows"] == ["Ready", "60%", "Queued"]
    assert (after["line"], after["pill"]) == ("55%", "3 episodes")


def test_a_card_that_leaves_with_the_focus_gives_it_to_the_next(out) -> None:
    assert out["patched"]["gone"]
    assert out["patched"]["focusTo"] == "radarr:Finchley"


def test_escape_closes_an_open_episode_list_before_it_folds(out) -> None:
    run = out["escapeFoldsAListFirst"]
    assert run["first"] == {"expanded": "false", "open": True, "on": True}
    assert run["then"] == {"open": False, "at": "/"}


def test_it_asks_every_2_s_while_open_and_seen_and_stops_when_closed(out) -> None:
    run = out["asking"]
    assert run["open"] == 3
    assert run["hidden"] == 0
    assert run["shown"] == 1, "asked at once when the tab is seen again"
    assert run["refreshed"] == 1, "and at once after an abandon"
    assert run["closed"] == 0


def test_with_nothing_downloading_the_row_stays_as_a_slim_card_that_opens_it(out) -> None:
    run = out["slimRow"]
    assert run["withSoon"]["cards"] == [["Nothing downloading", "2 coming soon"]]
    assert (run["withSoon"]["href"], run["withSoon"]["opens"]) == ("/?dl=1", True)
    assert run["opened"] == {"at": "/?dl=1", "open": True}
    assert run["none"]["cards"] == [["Nothing downloading", ""]]
    assert run["afterLast"]["cards"] == [["Nothing downloading", ""]]


def test_in_an_outage_the_slim_row_never_says_nothing_downloads(out) -> None:
    assert out["slimRow"]["outage"]["cards"] == [["Downloads not updating", "2 coming soon"]]


def test_it_grows_to_where_it_lands_below_the_bar_and_not_where_the_banner_ran_up(out) -> None:
    assert out["growsWhereItLands"]["pinned"] == "72px"


def test_a_tap_or_click_opens_it_with_no_ring_and_a_key_press_with_one(out) -> None:
    run = out["quietByPointer"]
    assert run["rowTap"] == {"open": True, "on": "watch-dl-title", "quiet": True}
    assert run["linkTap"] == {"open": True, "on": "watch-dl-title", "quiet": True}
    assert run["linkKey"] == {"open": True, "on": "watch-dl-title", "quiet": False}


def test_the_slim_row_learns_what_is_coming_soon_from_the_strip_without_asking_for_more(
    out,
) -> None:
    assert out["slimRow"]["asked"] == 0


def test_where_downloads_are_not_set_up_there_is_no_row(out) -> None:
    assert out["slimRow"]["unset"]["hidden"] is True
    assert out["slimRow"]["unset"]["cards"] == []


def test_downloads_not_set_up_are_said_by_role(out) -> None:
    states = out["states"]
    assert states["unsetAdmin"]["empty"] == [
        "Downloads aren’t set up",
        "Connect Radarr or Sonarr in Settings.",
        "/settings#setg-integrations",
    ]
    assert states["unsetViewer"]["empty"] == [
        "Downloads aren’t set up",
        "Ask whoever runs mediaman to set it up.",
        None,
    ]


def test_an_outage_is_said_by_role(out) -> None:
    states = out["states"]
    assert states["outageAdmin"]["note"] == "Can’t reach Radarr right now."
    assert states["refusedAdmin"]["empty"] == [
        "Downloads can’t be checked right now",
        "Check the details for Radarr in Settings.",
        "/settings#setg-integrations",
    ]
    assert states["outageViewer"]["count"] == "1 in progress · some may be missing"
    assert states["outageViewerNothing"]["empty"] == [
        "Downloads can’t be checked right now",
        "This page keeps trying.",
        None,
    ]
    assert states["caughtUp"]["empty"] == ["All caught up", "Nothing downloading right now.", None]
    assert states["caughtUp"]["count"] == "Nothing downloading · 1 coming soon"


def test_the_abandon_dialog_gives_the_focus_back_to_its_button_else_to_the_heading(out) -> None:
    run = out["abandonFocus"]
    assert run["modalOpen"] and run["back"]
    assert run["gone"] and run["then"] == "watch-dl-title"


def test_a_card_with_no_title_opens_downloading_from_the_row_and_is_no_link_in_it(out) -> None:
    run = out["cardWithoutTitle"]
    assert run["inRow"] == ["/?dl=1", True]
    assert run["inGrid"] == [None, False]


def test_a_card_takes_its_title_s_page_and_picture_from_the_row_only_when_one_title_matches(
    out,
) -> None:
    run = out["artFromTheRow"]
    assert run["ember"] == ["/watch#title=tmdb-movie-11", "/api/poster/1?kind=art"]
    assert [href for href, _ in run["twins"]] == [None, None]


def test_the_banner_stays_the_first_of_the_page_so_it_still_runs_up_under_the_bar(out) -> None:
    assert out["bannerFirst"] == {"open": "watch-banner", "folded": "watch-banner"}


def test_a_card_keeps_the_row_s_state_on_it_from_the_first_draw(out) -> None:
    assert out["groups"]["states"] == {
        "Windward": ["is-stuck", "is-looking"],
        "Honest Forger": ["is-queued"],
        "Quiet Engines": ["is-looking"],
        "Ember Coast": [],
    }


def test_stop_tracking_comes_after_the_poster_and_the_words(out) -> None:
    assert out["groups"]["stopLast"] is True


def test_an_episode_inside_a_pack_says_so(out) -> None:
    run = out["packEpisode"]
    assert run["words"] == ["In pack", "30%"]
    assert run["pack"] == [True, False]


def test_one_failed_asking_is_no_news_two_are_told_and_each_asks_later(out) -> None:
    run = out["failing"]
    assert run["once"] == {"note": None, "cards": 3}
    assert run["twice"] == "Can’t reach mediaman right now. This is how it last was."
    assert run["back"] is None
    assert run["gaps"] == [2000, 4000, 8000, 16000, 2000]


def test_a_slow_answer_is_told_at_20_s_and_given_up_at_120_s_for_another(out) -> None:
    run = out["slow"]
    assert run["before"] is None
    assert run["late"] == "Can’t reach mediaman right now. This is how it last was."
    assert run["hung"]["aborted"] == 1
    assert run["then"] == {"asked": 1, "note": None}


def test_before_the_first_answer_it_holds_placeholders_and_a_failure_says_so_at_once(
    out,
) -> None:
    run = out["firstLoad"]
    assert run["waiting"] == {
        "busy": "true",
        "empty": None,
        "skels": 3,
        "note": None,
        "count": "",
    }
    assert run["down"]["busy"] == "false"
    assert run["down"]["skels"] == 0
    assert run["down"]["empty"].startswith("Downloads can’t be checked right now")
    assert run["down"]["note"] == "Can’t reach mediaman right now."


def test_a_hidden_tab_does_not_ask_even_when_told_of_an_abandon(out) -> None:
    assert out["hiddenTab"]["asked"] == 0


def test_in_an_outage_the_head_never_says_nothing_downloads(out) -> None:
    states = out["states"]
    assert states["outageAdminNothing"]["count"] == (
        "Can’t reach Radarr, Sonarr and NZBGet · 1 coming soon"
    )
    assert states["refusedAdmin"]["count"] == "Can’t reach Radarr"
    assert states["outageViewerNothing"]["count"] == ""
    assert states["outageViewerSoon"]["count"] == "1 coming soon"


def test_a_wrapped_head_never_starts_a_line_with_the_dot_or_splits_coming_soon(out) -> None:
    raw = out["states"]["outageAdminNothing"]["raw"]
    assert raw == "Can’t reach Radarr, Sonarr and NZBGet · 1 coming soon"


def test_an_opener_under_a_title_s_page_steps_back_down_to_downloading(out) -> None:
    run = out["openerUnderATitle"]
    assert run == {
        "at": "/?dl=1",
        "top": "dl",
        "on": "watch-dl-title",
        "open": True,
        "log": ["close title"],
    }


def test_closed_and_opened_again_before_the_answer_it_grows_once_and_keeps_the_rows_place(
    out,
) -> None:
    run = out["reopenedBeforeTheAnswer"]
    # the first asking is given up with the fold; the second grows the placeholders at once
    assert run["again"] == {"open": True, "skels": 2, "asks": 2, "aborted": 1}
    assert run["filled"] == {"skels": 0, "cards": 3}
    assert run["grown"] is True
    assert run["left"] == 300
    assert run["open"] is False


def test_a_deep_link_focuses_the_heading_with_no_ring_and_a_press_with_one(out) -> None:
    assert out["deepLink"]["quiet"] is True
    assert out["opensFromTheRow"]["quiet"] is False


def test_a_title_that_is_done_leads_the_grid_and_the_grid_says_it_showed_it(out) -> None:
    got = out["readyLeads"]
    assert got["order"][:3] == ["# Ready to watch", "Harbour", "# Downloading"]
    # its card opens the title, not the grid again
    assert got["href"] == "/watch#title=tmdb-movie-7"
    # told only once it is on screen
    assert got["closed"] == 0
    assert got["told"] == ["radarr:Harbour"]
    # the head counts it beside what downloads
    assert got["count"] == "1 in progress · 1 ready to watch"


def test_with_no_answer_yet_a_tap_sets_the_address_and_grows_into_placeholders_at_once(
    out,
) -> None:
    run = out["growsBeforeTheAnswer"]
    assert run["tapped"]["at"] == "/?dl=1"
    assert run["tapped"]["open"] is True
    assert run["tapped"]["skels"] > 0
    assert run["tapped"]["busy"] == "true"
    assert run["tapped"]["empty"] is True
    assert run["rising"] > 0
    assert run["grown"] == {
        "rowsAway": True,
        "skels": run["tapped"]["skels"],
        "on": "watch-dl-title",
    }


def test_the_answer_replaces_the_placeholders_and_its_cards_fade_in(out) -> None:
    run = out["growsBeforeTheAnswer"]
    assert run["filled"] == {"skels": 0, "cards": 3, "busy": "false"}
    assert run["fades"] > 0


def test_placeholders_give_way_to_the_outage_header_on_a_failure_and_on_a_hang(out) -> None:
    run = out["skeletonThenOutage"]
    # a failure inside the moment a tap waits grows straight into the outage
    assert run["down"]["first"] == 0
    assert run["down"]["then"]["skels"] == 0
    assert run["down"]["then"]["empty"].startswith("Downloads can’t be checked right now")
    assert run["hung"]["waiting"] == {"skels": 2, "empty": None, "note": None}
    assert run["hung"]["late"]["skels"] == 0
    assert run["hung"]["late"]["empty"].startswith("Downloads can’t be checked right now")
    assert run["hung"]["late"]["note"] == "Can’t reach mediaman right now."
    assert run["hung"]["aborted"] == 1
    assert run["hung"]["then"]["skels"] == 0


def test_with_reduced_motion_the_placeholders_show_at_once_and_the_answer_lands_still(out) -> None:
    run = out["reducedBeforeTheAnswer"]
    assert run == {"open": True, "skels": run["skels"], "moved": 0, "cards": 3}
    assert run["skels"] > 0


def test_an_answer_within_a_moment_of_the_tap_flies_the_row_s_own_cards(out) -> None:
    run = out["raceFast"]
    assert run["at"] == "/?dl=1"
    assert run["skels"] == 0
    assert run["cards"] == 2
    assert run["placeholders"] == 0
    assert run["grown"] is True


def test_a_later_answer_flies_placeholders_out_of_the_row_and_fills_them(out) -> None:
    run = out["raceSlow"]
    assert run["at"] == "/?dl=1"
    assert run["skels"] == 2
    assert run["cards"] == 0
    assert run["placeholders"] == 2
    assert run["grown"] is True


def test_placeholders_take_the_filled_grid_s_layout_heading_line_and_cards(out) -> None:
    run = out["placeholderLayout"]
    assert run["waiting"]["kids"] == [
        "grid-group dlg-skel-head hidden",
        "dlg-skel hidden",
        "dlg-skel hidden",
    ]
    # the line under the title keeps its room, and says nothing
    assert run["waiting"]["count"] == 1
    assert run["waiting"]["countText"] == ""
    assert run["kids"][0] == "grid-group"


def test_placeholders_follow_the_row_s_groups_and_hold_the_buttons_their_cards_have(out) -> None:
    grouped = out["placeholderLayout"]["grouped"]
    # an admin's stuck film links to Radarr; a show has its episodes
    assert grouped == ["head", "card+buttons", "head", "card", "card+buttons"]


def test_the_answer_fades_the_placeholders_out_where_they_stood_and_then_drops_them(out) -> None:
    run = out["placeholderLayout"]
    assert run["filling"] == {
        "going": 3,
        "outs": 3,
        "count": 0,
        "countText": "2 in progress · 1 coming soon",
    }
    assert run["gone"] == 0


def test_an_answer_while_the_placeholders_fly_waits_for_them_to_land(out) -> None:
    run = out["answerMidGrow"]
    assert run["mid"] == {"skels": 2, "cards": 0}
    assert run["landed"] == {"skels": 0, "cards": 3}


def test_a_placeholder_holds_one_line_and_the_controls_its_card_will_have_by_role(out) -> None:
    run = out["placeholderBands"]
    # needs attention, downloading, a queued show, a queued film
    assert run["admin"] == ["line more", "line", "line eps more", "line more"]
    assert run["viewer"] == ["line", "line", "line eps", "line"]


def test_while_it_waits_the_grid_says_the_outage_the_row_already_says(out) -> None:
    run = out["placeholderBands"]
    assert run["rowNote"] == "Can’t sign in to Sonarr right now."
    assert run["gridNote"] == run["rowNote"]


def test_a_press_anywhere_on_a_card_but_its_controls_opens_its_title(out) -> None:
    run = out["opensTheTitle"]
    assert run["foot"] == ["movie-501"]
    assert run["episodes"] == ["tmdb-tv-9"]
    assert (run["pill"], run["more"]) == ([], [])
    assert run["soon"] == ["/#title=tmdb-movie-77", "movie", ["tmdb-movie-77"]]


def test_with_its_menu_open_a_press_on_the_card_only_shuts_the_menu(out) -> None:
    run = out["opensTheTitle"]
    assert (run["withMenu"], run["menuShut"]) == ([], "false")


def test_the_server_s_title_and_scores_win_over_the_row_s_and_none_opens_nothing(out) -> None:
    run = out["opensTheTitle"]
    assert run["ember"] == ["/#title=movie-501", "Rotten Tomatoes 93%IMDb 7.1"]
    assert (run["nameless"], run["namelessLink"]) == ([], None)


def test_escape_shuts_the_dialog_then_the_menu_then_the_episodes_then_the_grid(out) -> None:
    run = out["escapeOrder"]
    assert run["all"] == {"dialog": True, "menu": "false", "eps": True, "grid": True}
    assert run["dialog"] == {"dialog": False, "menu": "false", "eps": True, "grid": True}
    assert run["menuOpen"] == "true"
    assert run["menu"] == {"dialog": False, "menu": "false", "eps": True, "grid": True}
    assert run["menuFocus"] is True
    assert run["eps"] == {"dialog": False, "menu": "false", "eps": False, "grid": True}
    assert run["grid"]["grid"] is False


def test_the_confirm_names_the_title_and_how_long_it_has_been_searched_for(out) -> None:
    run = out["confirmSays"]
    assert run["abandon"]["who"][:2] == ["Quiet Engines", "Searched 10× since 20 Nov"]
    assert run["abandon"]["title"] == "Abandon the search?"
    assert run["abandon"]["label"] == "Abandon search"
    assert "Nothing is deleted from disk" in run["abandon"]["copy"]
    # with no start of its search known, no "since"
    assert run["noSince"] == [
        "Cobblestone",
        "Searched 3×",
        "/api/watch/img/tmdb/w342/Cobblestone.jpg",
    ]


def test_stop_tracking_asks_with_the_same_confirm_worded_for_it(out) -> None:
    stop = out["confirmSays"]["stop"]
    assert stop["who"][:2] == ["Lanterns", "Releases 8 Dec 2026"]
    assert (stop["title"], stop["label"]) == ("Stop tracking it?", "Stop tracking")


def test_the_confirm_says_abandoning_with_a_spinner_while_the_call_runs(out) -> None:
    run = out["confirmSays"]
    assert (run["busy"]["label"], run["busy"]["spin"]) == ("Abandoning…", True)
    assert run["closed"] is True


def test_a_card_that_goes_fades_and_shrinks_then_leaves(out) -> None:
    run = out["leaving"]["moving"]
    start, end = run["frames"]
    assert (start["opacity"], start["transform"]) == (1, "none")
    assert end["opacity"] == 0
    assert 0 < float(re.fullmatch(r"scale\(([\d.]+)\)", end["transform"])[1]) < 0.99
    assert run["ms"] > 0
    # still in its place while it fades, no longer counted, then gone
    assert run["during"] == {"there": True, "going": True, "cards": 1}
    assert run["gone"] is True


def test_with_reduced_motion_a_card_that_goes_only_fades(out) -> None:
    run = out["leaving"]["reduced"]
    assert run["frames"] == [{"opacity": 1}, {"opacity": 0}]
    assert (run["ms"], run["easing"]) == (150, "linear")
    assert run["gone"] is True


def test_a_ready_card_s_play_is_its_own_control_that_plays_and_opens_no_sheet(out) -> None:
    run = out["readyPlay"]
    assert run["play"] == {
        "tag": "A",
        "href": "/watch/play/12",
        "label": "Resume Harbour",
        "inLink": False,
        "hidden": None,
    }
    assert run["spans"] == 0, "no drawn Play left inside the card's link"
    assert run["pressed"] == {"opened": [], "followed": True}
    assert run["askedOnce"] is True


def test_a_ready_card_gets_its_play_once_the_library_catches_up(out) -> None:
    run = out["readyPlayLate"]
    assert run["first"] == 1
    assert run["notYet"] == {"play": False, "asks": 0}, "asked again sooner than 10 s"
    assert run["late"] == {"play": True, "asks": 1}


def test_a_ready_card_with_its_play_asks_no_more(out) -> None:
    assert out["readyPlayLate"]["after"] == 1


def test_a_ready_card_with_nothing_safe_to_play_shows_no_play(out) -> None:
    # an unsafe address, no play, and no preview at all
    assert out["readyPlay"]["noPlay"] == [False, False, False]


def test_a_ready_card_says_when_it_finished_and_its_size_and_only_what_it_knows(out) -> None:
    assert out["readyPlay"]["say"] == [
        "Finished 3 min ago · 14.2 GB",
        "2.5 GB",
        "Finished just now",
        "Now in your library",
    ]


def test_a_stale_menu_never_abandons_what_has_started_downloading_or_gone(out) -> None:
    for case, run in out["staleMenu"].items():
        assert run["opened"] is True, case
        assert run["open"] is False, case
        assert run["dialog"] is False, case


def test_a_card_s_menu_lines_its_right_edge_up_with_its_button(out) -> None:
    assert out["menuPlace"]["left"] == "692px"


def test_episodes_opened_by_a_click_take_the_focus_and_shut_on_escape_before_the_grid(
    out,
) -> None:
    run = out["escapeUnfocused"]
    assert run["focused"] is True
    assert run["first"] == {"expanded": "false", "open": True, "on": True}
    assert run["then"] is False


def test_escape_with_the_sheet_over_the_grid_shuts_confirm_sheet_menu_episodes_in_turn(
    out,
) -> None:
    run = out["escapeOrderWithSheet"]
    base = {"dialog": False, "sheet": False, "menu": "false", "eps": True, "grid": True}
    assert run["all"] == {**base, "dialog": True, "sheet": True}
    assert run["dialog"] == {**base, "sheet": True}
    assert run["sheet"] == base
    assert run["menuOpen"] == "true"
    assert run["menu"] == base
    assert run["eps"] == {**base, "eps": False}
    assert run["grid"]["grid"] is False


def test_coming_soon_draws_its_scores_where_the_server_sends_them(out) -> None:
    assert out["soonScores"] == ["Rotten Tomatoes 88%IMDb 7.4", None]
