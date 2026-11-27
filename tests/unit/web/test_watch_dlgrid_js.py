"""Downloading on Watch, grown out of its row at /?dl=1: the real scripts under node against a fake
DOM, with a fake history and a clock that is stepped."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "watch_dlgrid_harness.js"


@pytest.fixture(scope="module")
def out() -> dict:
    result = subprocess.run(
        ["node", str(HARNESS)], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    broken = {name: got["error"] for name, got in data.items() if "error" in got}
    assert not broken, broken
    return data


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


def test_it_grows_in_250_ms_and_folds_in_200(out) -> None:
    assert out["opensFromTheRow"]["grow"] == 250
    assert out["opensFromTheRow"]["fold"] == 200


def test_the_rows_above_go_up_and_those_below_go_down_as_the_row_s_cards_grow(out) -> None:
    run = out["grows"]
    assert run["above"] == ["translateY(-28px)", "translateY(-28px)"]
    assert run["below"] == ["translateY(44px)"]
    # cut to the row card's size at first, so no empty band shows while it grows
    assert run["card"] == {"from": True, "to": "none", "ms": 250}
    assert run["title"] == [True], "the row's name travels into the header"


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
    assert cards["Honest Forger"]["facts"] == "Waiting its turn · 3.2 GB"


def test_each_card_says_what_the_old_page_said_of_its_download(out) -> None:
    cards = out["groups"]["cards"]
    assert cards["Ember Coast"]["line"] == "43% · 5 min left"
    assert cards["Ember Coast"]["facts"] == "4.1 GB of 9.6 GB"
    assert cards["Sundial"]["line"] == "Waiting to import"
    assert cards["Sundial"]["facts"] == "2 GB of 2 GB · Post-processing…"
    assert cards["Quiet Engines"]["seen"] == "Searched 11× · next attempt in ~24h"
    assert cards["Cobblestone"]["seen"] == "Searched once · next attempt in 1m"


def test_an_admin_is_told_what_is_wrong_and_what_comes_of_it(out) -> None:
    cards = out["groups"]["cards"]
    windward = cards["Windward"]
    assert windward["line"] == "Needs attention"
    assert windward["why"] == "Radarr saysImport failed, path does not exist"
    assert (windward["fix"], windward["fixKind"]) == (
        "Fix it in Radarr: Activity → Queue",
        ["is-act"],
    )
    tallow = cards["Tallow Hill"]
    assert (tallow["fix"], tallow["fixKind"]) == ("mediaman will look for another copy", ["is-ok"])


def test_an_admin_is_given_the_link_into_radarr_only_to_a_web_address(out) -> None:
    cards = out["groups"]["cards"]
    link = [
        "View in Radarr ↗",
        None,
        "http://radarr.local/movie/1",
        "_blank",
        "noopener noreferrer",
    ]
    assert cards["Windward"]["pills"] == [link]
    assert cards["Cobblestone"]["pills"] == [], "javascript: is no link"
    assert cards["Ember Coast"]["pills"] == [], "nor while it downloads"


def test_every_abandon_and_stop_tracking_button_is_named_by_its_title(out) -> None:
    cards = out["groups"]["cards"]
    assert [p[:2] for p in cards["Quiet Engines"]["pills"]][1] == [
        "Abandon search",
        "Abandon search: Quiet Engines",
    ]
    assert cards["Lanterns"]["pills"] == [
        ["Stop tracking", "Stop tracking: Lanterns", None, None, None]
    ]
    # what the abandon dialog reads off the button
    assert out["groups"]["trigger"] == ["radarr:Quiet Engines", "7", "movie", "Quiet Engines", "[]"]
    assert out["groups"]["stop"] == "1"


def test_only_a_download_the_server_lets_go_of_has_an_abandon_button(out) -> None:
    cards = out["groups"]["cards"]
    with_abandon = [
        t for t, c in cards.items() if any(p[0] == "Abandon search" for p in c["pills"])
    ]
    assert with_abandon == ["Quiet Engines"]


def test_a_viewer_is_never_drawn_a_reason_a_link_or_an_abandon_button_even_if_sent_one(
    out,
) -> None:
    run = out["viewer"]
    assert (run["triggers"], run["links"], run["stops"], run["why"]) == (0, 0, 0, [])
    assert not run["modal"]
    assert run["fix"] == [
        "Won’t finish until an admin sorts it out",
        "mediaman will look for another copy",
    ]
    assert run["seen"] == ["Searched 11× · next attempt in ~24h"]


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
    assert run["asked"] == 1, "what is coming soon is asked once"


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


def test_before_the_first_answer_it_says_it_is_checking_and_a_failure_says_so_at_once(
    out,
) -> None:
    run = out["firstLoad"]
    assert run["waiting"] == {
        "busy": "true",
        "empty": "Checking downloads…",
        "note": None,
        "count": "",
    }
    assert run["down"]["busy"] == "false"
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
    assert run["grown"] is True
    assert run["left"] == 300
    assert run["open"] is False


def test_a_deep_link_focuses_the_heading_with_no_ring_and_a_press_with_one(out) -> None:
    assert out["deepLink"]["quiet"] is True
    assert out["opensFromTheRow"]["quiet"] is False
