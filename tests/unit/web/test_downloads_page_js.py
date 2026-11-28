"""The abandon dialog, the shared modal and the re-authenticate dialog, with the downloads API
and the download rows' sheet.

The real scripts run under node against a small fake DOM, so these check what
a person ends up seeing, not how the scripts are written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router as downloads_router
from tests.helpers.factories import insert_admin_user
from tests.unit.web._css import style

HARNESS = Path(__file__).parent / "js" / "downloads_harness.js"
SHEET = "_dl.css"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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


def test_confirm_says_it_is_working_and_recovers_on_failure() -> None:
    out = _run("abandon")
    assert out["idle"] == "Abandon search"
    assert out["working"] == "Abandoning…"
    assert out["disabled"] == "true"
    assert out["after"] == "Abandon search"
    assert out["reenabled"] == "false"


def test_the_confirm_shows_only_a_poster_of_mediamans_own() -> None:
    assert _run("abandonPoster") == {
        "/p/1.jpg": "/p/1.jpg",
        "//host/x.jpg": None,
        "/\\host/x.jpg": None,
        "https://host/x.jpg": None,
        "/a b.jpg": None,
    }


def test_a_successful_abandon_closes_the_dialog_and_refreshes_the_page() -> None:
    out = _run("abandonSuccess")
    assert out["url"] == "/api/downloads/m1/abandon"
    assert out["closes"] == 1
    assert out["refreshes"] == 1
    assert out["errors"] == 0


def test_an_abandon_sends_the_arr_id_of_the_button_that_opened_it() -> None:
    assert _run("abandonSendsTheArrId") == {"seasons": [], "arr_id": 21}


def test_abandoning_a_title_already_gone_closes_the_dialog_and_refreshes_the_page() -> None:
    out = _run("abandonGone")
    assert (out["closes"], out["refreshes"], out["errors"]) == (1, 1, 0)
    assert len(out["toasts"]) == 1
    text, variant = out["toasts"][0]
    assert "Already gone" in text and "Tm1" in text
    assert variant == "info"


def test_abandoning_a_title_no_longer_stuck_closes_the_dialog_and_says_so() -> None:
    out = _run("abandonNoLongerStuck")
    assert (out["closes"], out["refreshes"], out["errors"]) == (1, 1, 0)
    assert len(out["toasts"]) == 1
    text, variant = out["toasts"][0]
    assert "Tm1" in text and "no longer stuck searching" in text
    assert "Reload" not in text
    assert variant == "info"


def test_a_failed_abandon_of_one_item_leaves_the_next_dialog_alone() -> None:
    out = _run("abandonStaleFailure")
    assert out["before"] == {"label": "Abandon search", "disabled": "false"}
    assert out["after"] == out["before"]
    assert out["errors"] == 1


def test_a_finished_abandon_of_one_item_does_not_close_the_next_dialog() -> None:
    out = _run("abandonStaleSuccess")
    assert out["closes"] == 0
    assert out["refreshes"] == 1
    assert out["b"] == {"label": "Abandon search", "disabled": "false"}


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
    assert out["aired"] == {"listed": False, "label": "Abandon search", "disabled": "false"}
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


def test_the_downloads_api_says_a_viewers_outage_once(app_factory, conn) -> None:
    insert_admin_user(conn, username="person", role="viewer")
    app = app_factory(downloads_router, conn=conn)
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
        answer = client.get("/api/downloads")
    body = answer.json()
    assert body["subtitle"] == ""
    assert body["outage"] == "Downloads aren't available"
    assert "Radarr" not in answer.text


def test_in_forced_colours_a_row_has_the_edge_the_top_card_has() -> None:
    forced = "(forced-colors: active)"
    edge = style(".dl-hero", SHEET, forced)["border"]
    assert "CanvasText" in edge
    assert style(".dl-row", SHEET, forced).get("border") == edge


def test_a_queued_episode_reads_as_a_queued_row_does() -> None:
    # a queued row's pill has no state of its own and is drawn as the plain pill
    episode, row = style(".dl-ep-status-pill.queued", SHEET), style(".dl-state-pill", SHEET)
    assert (episode["color"], episode["background"]) == (row["color"], row["background"])


def test_a_long_title_wraps_on_a_narrow_phone() -> None:
    title = ".dl-compact > .dl-row .dl-row-title"
    assert style(title, SHEET, "(max-width: 374.98px)")["white-space"] == "normal"
