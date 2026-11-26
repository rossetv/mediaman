"""The /downloads page scripts keep the page in step with each poll.

The real scripts run under node against a small fake DOM, so these check what
the page ends up showing, not how the scripts are written.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router as downloads_router
from tests.helpers.factories import insert_admin_user

HARNESS = Path(__file__).parent / "js" / "downloads_harness.js"
CSS = Path(__file__).parents[3] / "src/mediaman/web/static/css"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_page_follows_the_poll_payload() -> None:
    out = _run("page")
    # hero and queue come back after the page went empty; a new item gets a row
    assert out["afterRefill"] == {"hero": 1, "rows": ["q2"]}
    assert out["withNew"] == ["q2", "q3"]
    # the abandon button appears with the payload, carries stuck seasons, and goes again
    assert out["shown"]["hero"] == 1
    assert out["shown"]["row"] == 1
    assert json.loads(out["shown"]["stuck"]) == [{"number": 2, "missing_episodes": 3}]
    assert out["hidden"] == {"hero": 0, "row": 0}
    # a departed row is removed even when no animation event arrives
    assert out["afterExit"] == ["q3"]


def test_a_departed_row_stays_until_its_height_has_closed() -> None:
    out = _run("rowLeave")
    assert out["during"]["rows"] == ["q1", "q2"]
    assert "is-folded" in out["during"]["q1"]["cls"]
    assert out["during"]["q1"]["height"] == "0px"
    assert out["afterFade"] == ["q1", "q2"]
    assert out["afterHeight"] == ["q2"]


def test_a_row_that_comes_back_while_closing_stays() -> None:
    out = _run("rowReturns")
    assert out["rows"] == ["q1", "q2"]
    assert out["q1"] == {"cls": "dl-compact", "height": ""}


def test_with_reduced_motion_a_departed_row_goes_at_once() -> None:
    assert _run("rowLeaveReduced")["rows"] == ["q2"]


def test_hero_and_queue_close_in_place_while_the_empty_state_opens() -> None:
    out = _run("emptyState")
    # the old sections keep their content while they close
    assert out["during"]["hero"] == 1
    assert out["during"]["queue"] == 2
    assert "is-folded" in out["during"]["heroFold"]["cls"]
    # the empty state grows in rather than appearing at full height
    assert out["during"]["empty"] == 1
    assert out["during"]["emptyFold"] == {"cls": "dl-fold", "height": "50px"}
    assert out["after"] == {
        "hero": 0,
        "queue": 0,
        "empty": 1,
        "emptyFold": {"cls": "", "height": ""},
    }


def test_in_an_outage_the_page_shows_what_the_server_lists_and_never_reads_as_all_caught_up() -> (
    None
):
    out = _run("outage")
    # what the answer leaves out goes, as any row does; the card says why
    assert out["gone"] == {
        "hero": 0,
        "queue": 0,
        "upcoming": 0,
        "cards": ["Downloads can't be checked right now"],
    }
    # and comes back as a new row does when the service answers
    assert out["back"] == {"hero": 1, "queue": 1, "upcoming": 1}
    assert [len(c) for c in out["cards"]] == [1, 1, 1]
    assert out["cards"][0] == out["cards"][2] == ["All caught up"]
    assert out["cards"][1] != ["All caught up"]


def test_an_admin_and_a_viewer_are_shown_the_same_rows_in_an_outage() -> None:
    out = _run("outageRows")
    assert out["admin"] == out["viewer"]
    assert out["viewer"]["during"] == {
        "hero": ["sonarr:h1"],
        "queue": ["sonarr:q2"],
        "upcoming": ["sonarr:u2"],
    }
    assert sorted(out["viewer"]["back"]["queue"]) == ["radarr:q1", "sonarr:q2", "sonarr:q3"]


def test_through_an_outage_rows_stay_in_order_of_state() -> None:
    # a new row takes its place in the server's list, within its state
    assert _run("outageOrder")["rows"] == ["Other.2024", "sonarr:q4", "sonarr:q2"]


def test_what_a_service_answers_in_part_is_drawn_from_the_answer() -> None:
    out = _run("outagePart")
    assert out["rows"] == ["radarr:h1", "radarr:u1"]
    assert out["pct"] == "50%"
    assert out["upcoming"] == []


def test_a_poll_update_keeps_the_size_of_the_state_pill() -> None:
    out = _run("pillSize")
    assert "dl-state-pill--xs" in out["before"]
    assert out["after"].split() == ["dl-state-pill", "dl-state-pill--xs", "dl-state-almost_ready"]


def test_the_recent_section_folds_out_when_it_empties() -> None:
    out = _run("recentLeaves")
    assert "is-folded" in out["during"]["cls"]
    assert out["during"]["height"] == "0px"
    # coming back while it closes opens it again
    assert "is-folded" not in out["back"]["cls"]
    assert out["gone"] is True


def test_with_reduced_motion_the_recent_section_goes_at_once() -> None:
    out = _run("recentLeavesReduced")
    assert out["during"] is None
    assert out["boxEmpty"] is True


def test_an_episode_list_folds_open_and_shut() -> None:
    out = _run("episodes")
    assert out["opening"]["hidden"] is False
    assert "dl-fold" in out["opening"]["cls"]
    assert "dl-fold" not in out["open"]["cls"]
    assert out["closing"] is False
    assert out["closed"] is True


def test_the_shared_modal_plays_its_exit_before_it_hides() -> None:
    out = _run("modal")
    assert out["initial"] == {"hidden": True, "closing": False, "aria": None}
    assert out["opened"] == {"hidden": False, "closing": False, "aria": "false"}
    # closing is decided at once: focus is handed back before the exit ends
    assert out["closing"] == {"hidden": False, "closing": True, "aria": "true"}
    assert out["a11yAtClose"] == ["opened", "closed"]
    # the sheet's own animation does not end the exit; the backdrop's does
    assert out["sheetEnd"] == out["closing"]
    assert out["closed"] == {"hidden": True, "closing": False, "aria": "true"}
    # with no animationend, the fallback timer ends it
    assert out["afterTimer"] == {"hidden": True, "closing": False, "aria": "true"}
    assert out["plainShown"] is False


def test_reopening_a_closing_modal_cancels_the_exit() -> None:
    out = _run("modalReopen")
    assert out["reopened"] == {
        "hidden": False,
        "closing": False,
        "aria": "false",
        "timers": 0,
        "listeners": 0,
    }
    assert out["afterLateEnd"] == {"hidden": False, "closing": False, "aria": "false"}
    assert out["closingAgain"]["closing"] is True
    assert out["closed"]["hidden"] is True


def test_the_shared_modal_hides_at_once_under_reduced_motion() -> None:
    out = _run("modalReduced")
    assert out == {"opened": False, "hidden": True, "closing": False, "aria": "true", "timers": 0}


def test_the_watch_admin_dialogs_fade_out_before_they_hide() -> None:
    out = _run("modalKeepOverlay")
    assert out["opened"] == {"active": True, "closing": False}
    # still shown (and still `active`) while the exit plays
    assert out["closing"] == {"active": True, "closing": True}
    assert out["closed"] == {"active": False, "closing": False}
    # reopening during the exit cancels it and leaves no timer behind
    assert out["reopened"] == {"active": True, "closing": False, "timers": 0}
    # reduced motion hides at once, with no timer
    assert out["reduced"] == {"active": False, "closing": False, "timers": 0}


def test_poll_now_during_a_poll_runs_another_straight_after() -> None:
    assert _run("poll")["calls"] == 2


def test_recent_grid_is_rebuilt_only_when_it_changes() -> None:
    out = _run("recent")
    assert out["kept"] is True
    assert out["count"] == 3


def test_a_show_is_called_ready_only_while_it_is_known_to_be() -> None:
    out = _run("recent")
    ready = ["Ready to watch", True]
    assert out["part"] == [["Some episodes ready", False], ready]
    assert out["unknown"] == [["In your library soon", False], ready]
    assert out["whole"] == [ready, ready]


def test_confirm_says_it_is_working_and_recovers_on_failure() -> None:
    out = _run("abandon")
    assert out["idle"] == "Abandon"
    assert out["working"] == "Abandoning…"
    assert out["disabled"] == "true"
    assert out["after"] == "Abandon"
    assert out["reenabled"] == "false"


def test_rebuilt_hero_shows_the_same_stats_as_the_server_rendered_one() -> None:
    out = _run("hero")
    assert out["full"] == ["30% complete", "1 GB of 3 GB", "5 min"]
    assert out["noEta"] == ["30% complete", "1 GB of 3 GB"]
    assert out["noSize"] == ["30% complete", " of ", "5 min"]


def test_a_successful_abandon_closes_the_dialog_and_refreshes_the_page() -> None:
    out = _run("abandonSuccess")
    assert out["url"] == "/api/downloads/m1/abandon"
    assert out["closes"] == 1
    assert out["refreshes"] == 1
    assert out["errors"] == 0


def test_an_abandon_names_the_very_title_its_button_is_of() -> None:
    out = _run("abandonNamesTheTitle")
    assert out["built"] == ["h 10", "q 11", "u 12"]
    assert out["updated"] == ["h 20", "q 21", "u 22"]
    assert out["sent"] == {"seasons": [], "arr_id": 21}


def test_abandoning_a_title_already_gone_closes_the_dialog_and_refreshes_the_page() -> None:
    out = _run("abandonGone")
    assert (out["closes"], out["refreshes"], out["errors"]) == (1, 1, 0)
    assert len(out["toasts"]) == 1
    text, variant = out["toasts"][0]
    assert "Already gone" in text and "Tm1" in text
    assert variant == "info"


def test_a_failed_abandon_of_one_item_leaves_the_next_dialog_alone() -> None:
    out = _run("abandonStaleFailure")
    assert out["before"] == {"label": "Abandon", "disabled": "false"}
    assert out["after"] == out["before"]
    assert out["errors"] == 1


def test_a_finished_abandon_of_one_item_does_not_close_the_next_dialog() -> None:
    out = _run("abandonStaleSuccess")
    assert out["closes"] == 0
    assert out["refreshes"] == 1
    assert out["b"] == {"label": "Abandon", "disabled": "false"}


def test_an_item_with_a_request_running_cannot_be_abandoned_twice() -> None:
    out = _run("abandonReopen")
    assert out["posts"] == 1
    assert out["reopened"] == {"label": "Abandoning…", "disabled": "true"}
    assert out["switchesLocked"] == ["true", "true"]
    assert out["closes"] == 1


def test_season_switches_are_locked_while_abandoning_and_failure_shows_the_current_count() -> None:
    out = _run("abandonSwitchesLocked")
    assert out["picked"] == "Abandon 2 seasons"
    assert out["oneLeft"] == "Abandon 1 season"
    assert out["during"] == {"label": "Abandoning…", "on": [False, True]}
    assert out["after"] == {"label": "Abandon 1 season", "disabled": "false"}


def test_the_subtitle_folds_away_and_grows_back_in_place() -> None:
    out = _run("subtitle")
    assert out["shown"] == {"text": "2 downloading", "cls": "", "height": ""}
    # it stays attached, folding, so what sits below slides instead of jumping
    assert out["folding"]["attached"] is True
    assert "is-folded" in out["folding"]["cls"]
    # coming back while it folds grows it again with the new text
    assert out["back"]["text"] == "1 downloading"
    assert "is-folded" not in out["back"]["cls"]
    assert out["removed"] is True


def test_escape_closes_a_modal_every_time_it_is_opened() -> None:
    out = _run("modalEscape")
    assert out["secondOpen"] == "true"
    assert out["thirdOpen"] == "true"
    assert out["listeners"] == 1


def test_the_reauth_dialog_leaves_through_the_shared_fade() -> None:
    out = _run("reauth")
    assert out["opened"] == {"hidden": False, "closing": False}
    # cancelling starts the fade and reports the cancel, the node stays until it ends
    assert out["closing"] == {"attached": True, "closing": True, "cancelled": 1}
    assert out["gone"] == 0
    assert out["cancelledAfterEsc"] == 2


def test_closing_the_reauth_dialog_while_the_password_is_checked_runs_nothing() -> None:
    out = _run("reauthCancelDuringCheck")
    assert out["calls"] == 1
    assert out["code"] == "reauth_cancelled"
    assert out["value"] is None


def test_a_second_reauth_cancels_the_first() -> None:
    out = _run("reauthSecond")
    assert out["first"] == "reauth_cancelled"
    assert out["second"] is None
    assert out["dialogs"] == 1


def test_the_season_picker_is_hidden_unless_a_show_has_several_stuck_seasons() -> None:
    out = _run("abandonList")
    assert out == {"movie": True, "picker": False, "movieAgain": True}


def test_a_lone_season_still_to_air_is_abandoned_only_when_picked() -> None:
    out = _run("abandonOneSeason")
    assert out["unaired"] == {
        "listed": True,
        "on": [False],
        "label": "Abandon",
        "disabled": "true",
    }
    assert out["sentUnticked"] == 0
    assert out["aired"] == {"listed": False, "label": "Abandon", "disabled": "false"}
    assert out["seasons"] == [[2], [3]]


def test_the_season_picker_ticks_only_seasons_with_missing_aired_episodes() -> None:
    out = _run("abandonSeasons")
    assert out["rows"] == [
        ["Specials", "4 missing episodes", False, "false"],
        ["Season 1", "1 missing episode", True, "true"],
        ["Season 2", "6 missing episodes", True, "true"],
        ["Season 3", "No missing episodes", False, "false"],
        ["Season 4", "Not aired yet", False, "false"],
    ]
    assert out["label"] == "Abandon 2 seasons"


def test_the_stop_dialog_names_the_service_and_claims_nothing_about_release_or_library() -> None:
    words = _run("abandonWords")
    assert "Radarr" in words["movie upcoming"] and "Sonarr" not in words["movie upcoming"]
    assert "Sonarr" in words["series upcoming"] and "Radarr" not in words["series upcoming"]
    assert "Sonarr" in words["series"] and "Radarr" in words["movie"]
    for text in words.values():
        assert "released" not in text and "library" not in text


def test_a_failed_stop_tracking_says_stop_tracking_and_keeps_the_servers_words() -> None:
    assert _run("abandonFailureWords") == [
        "Couldn’t stop tracking. Try again.",
        "Couldn’t abandon. Try again.",
        "Radarr is down.",
    ]


def test_with_no_radarr_and_no_sonarr_the_page_says_downloads_are_not_set_up() -> None:
    out = _run("notSetUp")
    assert out["admin"] == {
        "h3": "Downloads aren’t set up",
        "p": "Connect Radarr or Sonarr in Settings.",
        "link": "/settings#setg-integrations",
    }
    assert out["viewer"] == {
        "h3": "Downloads aren’t set up",
        "p": "Ask whoever runs mediaman to set it up.",
        "link": None,
    }
    # one service set up, or no answer about it: the page is as it was
    for key in ("films", "shows", "missing"):
        assert out[key]["h3"] == "All caught up"


def test_the_top_cards_fading_copy_is_laid_out_in_the_cards_own_box() -> None:
    out = _run("heroSwap")
    assert out["moving"]["ghosts"] == [""]
    assert out["reduced"]["ghosts"] == []
    css = (CSS / "_dl.css").read_text()
    rule = css.split(".dl-hero-ghost__card {", 1)[1].split("}", 1)[0]
    assert "inset: 0;" in rule and "min-height" not in rule and "height:" not in rule


def test_the_new_content_of_the_top_card_waits_for_the_old_to_fade() -> None:
    out = _run("heroSwap")
    assert out["moving"]["swapping"] and not out["moving"]["later"]
    assert not out["reduced"]["swapping"]
    css = (CSS / "_dl.css").read_text()
    fade = re.search(r"\.dl-hero-ghost \{[^}]*animation: dl-hero-out var\(--(dur[-\w]*)\)", css)
    came = re.search(
        r"\.dl-hero\.is-swapping \.dl-hero-content \{ animation: dl-hero-in \S+ \S+ var\(--(dur[-\w]*)\) backwards",
        css,
    )
    assert fade and came and fade[1] == came[1], "it begins as the old has gone"


def test_a_row_that_changes_place_slides_with_those_it_passes_unless_motion_is_off() -> None:
    out = _run("rowSlide")
    assert out["moving"]["order"] == ["q3", "q1", "q2"]
    assert out["moving"]["sliding"] == [True, True, False]
    # a row put back in the list would play its entrance again, over the slide
    assert out["moving"]["entering"] == [False, False, False]
    assert out["reduced"]["sliding"] == [False, False, False]
    css = (CSS / "_dl.css").read_text()
    leave = re.search(
        r"\.dl-fold \{[^}]*transition: height (var\(--dur-slow\) var\(--ease-out\))", css
    )
    assert (
        leave
        and f"transform {leave[1]}" in css.split(".dl-compact.is-sliding {", 1)[1].split("}", 1)[0]
    )


def test_rows_below_a_row_that_grows_slide_down_unless_motion_is_off() -> None:
    out = _run("rowGrows")
    assert out["moving"]["sliding"] == [False, True]
    assert out["moving"]["from"] == ["", "translateY(-43px)"]
    assert out["reduced"]["sliding"] == [False, False]


def test_a_row_that_moves_again_mid_slide_starts_from_where_it_is_seen() -> None:
    assert _run("slideAgain") == [["q2", "translateY(40px)"], ["q1", "translateY(-100px)"]]


def test_rows_that_come_back_take_the_servers_order() -> None:
    out = _run("returnOrder")
    assert out["upcoming"] == ["u1", "u2", "u3"]
    assert out["queue"] == ["q1", "q2", "q3"]


def test_a_viewers_outage_with_nothing_in_progress_is_said_once() -> None:
    out = _run("viewerOutageLine")
    assert out["viewer"] == []
    assert out["viewerSoon"] == ["1 coming soon"]
    assert out["admin"] == ["Can't reach Radarr"]


def test_the_server_says_a_viewers_outage_once(app_factory, conn) -> None:
    insert_admin_user(conn, username="person", role="viewer")
    app = app_factory(downloads_router, conn=conn)
    app.state.templates = Jinja2Templates(directory=str(CSS.parents[1] / "templates"))
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "person"))
    down = {
        "hero": None,
        "queue": [],
        "upcoming": [],
        "recent": [],
        "subtitle": "Can't reach Radarr",
        "unreachable": ["Radarr"],
        "refused": [],
        "outage": "Can't reach Radarr",
        "downloads_set_up": {"movies": True, "shows": True},
    }
    with patch("mediaman.web.routes.downloads.build_downloads_response", return_value=down):
        page = client.get("/downloads").text
    header = page.split('class="ph"', 1)[1].split("</div>", 1)[0]
    assert "<p>" not in header
    assert "Downloads can&#39;t be checked right now" in page
    assert "Radarr" not in page


def test_in_forced_colours_a_row_has_the_edge_the_top_card_has() -> None:
    forced = (CSS / "_dl.css").read_text().split("@media (forced-colors: active) {", 1)[1]
    edges = re.findall(r"([^{}]+)\{[^}]*border: 1px solid CanvasText", forced)
    assert any(".dl-hero" in sel for sel in edges)
    assert any(re.search(r"(^|,)\s*\.dl-row\s*(,|$)", sel) for sel in edges)


def test_a_poster_that_is_missing_or_does_not_load_is_stood_for_by_a_mark() -> None:
    out = _run("posterMark")
    assert out["hero"] == [["IMG.", "SPAN.dl-poster-mark"]]
    assert out["rows"] == [["SPAN.dl-poster-mark"], ["IMG.", "SPAN.dl-poster-mark"]]
    css = (CSS / "_dl.css").read_text()
    assert "img:not([data-broken]) ~ .dl-poster-mark { visibility: hidden; }" in css


def test_the_queues_heading_is_true_of_its_rows() -> None:
    assert _run("queueHeading") == {"queued": "Up next", "mixed": "Also in progress"}


def test_a_refused_key_sends_an_admin_to_settings_and_does_not_say_it_keeps_trying() -> None:
    out = _run("refusedKey")
    link = "/settings#setg-integrations"
    assert out["admin"] == {"p": "Check the details for Sonarr in Settings.", "link": link}
    assert out["two"]["p"] == "Check the details for Radarr and Sonarr in Settings."
    assert out["down"] == out["viewer"] == {"p": "This page keeps trying.", "link": None}


def test_a_viewer_is_told_rows_may_be_missing_not_which_service_is_down() -> None:
    out = _run("viewerLine")
    assert out["viewer"] == "2 items in progress · 1 coming soon · some may be missing"
    assert out["admin"] == "server words"


def test_only_an_admins_rows_hold_a_link_into_radarr_or_sonarr() -> None:
    assert _run("arrLink") == {"admin": 2, "viewer": 0}


def test_a_queued_episode_reads_as_a_queued_row_does() -> None:
    css = (CSS / "_dl.css").read_text()
    pill = css.split(".dl-ep-status-pill.queued {", 1)[1].split("}", 1)[0]
    assert "warning" not in pill


def test_a_long_title_wraps_on_a_narrow_phone() -> None:
    css = (CSS / "_dl.css").read_text()
    narrow = css.split("@media (max-width: 374.98px) {", 1)[1].split("\n}\n", 1)[0]
    assert "white-space: normal" in narrow.split(".dl-row-title {", 1)[1].split("}", 1)[0]


def test_a_redrawn_abandon_button_is_named_by_its_title_and_follows_a_new_one() -> None:
    out = _run("abandonNames")
    assert out["built"] == [
        "Abandon search: Hero A | Abandon search",
        "Abandon search: Queue A | Abandon search",
        "Stop tracking: Soon A | Stop tracking",
    ]
    assert [n.replace(" B", " A") for n in out["updated"]] == out["built"]
    assert all(" B |" in n for n in out["updated"])
