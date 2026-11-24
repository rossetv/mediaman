"""The /downloads page scripts keep the page in step with each poll.

The real scripts run under node against a small fake DOM, so these check what
the page ends up showing, not how the scripts are written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "downloads_harness.js"


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


def test_an_outage_keeps_live_sections_and_never_reads_as_all_caught_up() -> None:
    out = _run("outage")
    assert out["held"] == {"hero": 1, "queue": 1, "upcoming": 1, "cards": []}
    assert out["back"] == {"hero": 0, "queue": 0, "upcoming": 0}
    # a download of a service that does answer is really over
    assert out["finishedElsewhere"] == 0
    assert [len(c) for c in out["cards"]] == [1, 1, 1]
    assert out["cards"][0] == out["cards"][2] == ["All caught up"]
    assert out["cards"][1] != ["All caught up"]


def test_an_outage_holds_only_the_rows_of_the_service_that_is_down() -> None:
    out = _run("outageRows")
    # the rest of the list goes on as usual
    assert out["some"] == {
        "hero": ["sonarr:h1"],
        "queue": ["radarr:q1", "sonarr:q2"],
        "upcoming": ["radarr:u1", "sonarr:u2"],
    }
    # a row that became the hero is not shown twice
    assert out["promoted"] == {
        "hero": ["sonarr:q2"],
        "queue": ["radarr:q1"],
        "upcoming": ["radarr:u1"],
    }
    # nothing of a service that answers is kept
    assert out["nothingElse"] == {"hero": [], "queue": ["radarr:q1"], "upcoming": ["radarr:u1"]}
    assert out["over"] == {"hero": [], "queue": [], "upcoming": []}


def test_a_hero_whose_service_went_down_stays_as_a_row_until_it_is_back() -> None:
    out = _run("outageRows")
    assert out["displaced"] == {"hero": ["sonarr:q2"], "queue": ["radarr:h1"], "upcoming": []}
    assert out["stays"] == out["displaced"]
    assert out["back"] == {"hero": ["radarr:h1"], "queue": ["sonarr:q2"], "upcoming": []}
    # one that was over before the outage does not come back
    assert out["ended"] == {"hero": ["sonarr:q9"], "queue": [], "upcoming": []}


def test_a_held_download_that_nzbget_still_lists_is_shown_once() -> None:
    assert _run("outageNzb")["rows"] == [
        "radarr:q1",
        "sonarr:q3",
        "Dune.Part.Two.2024",
        "Edit.2020",
    ]


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


def test_confirm_says_it_is_working_and_recovers_on_failure() -> None:
    out = _run("abandon")
    assert out["idle"] == "Abandon"
    assert out["working"] == "Abandoning…"
    assert out["disabled"] == "true"
    assert out["after"] == "Abandon"
    assert out["reenabled"] == "false"


def test_rebuilt_hero_shows_the_same_stats_as_the_server_rendered_one() -> None:
    out = _run("hero")
    assert out["full"] == ["30% complete", "1 GB of 3 GB", "ETA 5 min"]
    assert out["noEta"] == ["30% complete", "1 GB of 3 GB"]
    assert out["noSize"] == ["30% complete", " of ", "ETA 5 min"]


def test_a_successful_abandon_closes_the_dialog_and_refreshes_the_page() -> None:
    out = _run("abandonSuccess")
    assert out["url"] == "/api/downloads/m1/abandon"
    assert out["closes"] == 1
    assert out["refreshes"] == 1
    assert out["errors"] == 0


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


def test_single_download_page_shows_an_eta_that_arrives_on_a_later_poll() -> None:
    result = subprocess.run(
        ["node", str(HARNESS.with_name("download_page_harness.js"))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["noEtaYet"] == ["30% complete", "1 GB of 3 GB"]
    assert out["arrives"] == ["30% complete", "1 GB of 3 GB", "ETA 5 min"]
    assert out["changes"] == ["30% complete", "1 GB of 3 GB", "ETA 4 min"]
    assert out["clears"] == ["30% complete", "1 GB of 3 GB"]


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
