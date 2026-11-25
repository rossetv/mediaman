"""All titles and the admin actions on its cards, run under node against a fake DOM.

Nothing here reloads the page: every action ends with the grid changed in place.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_library_harness.js"


# The keep the server answers a save made.
MADE = {
    "action": "snoozed",
    "execute_at": "2026-12-24T12:00:00+00:00",
    "kept_at": "2026-11-24T12:00:00+00:00",
}
WATCH_KEEP = "/api/watch/keep"
OPEN_CHANGED = "changed while this was open"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_keeping_a_scheduled_movie_shows_it_in_place_and_undo_brings_the_date_back() -> None:
    out = _run("keepScheduled")
    assert out["call"] == ["post", "/api/watch/keep", {"media_item_id": "m1", "days": 30}]
    assert (out["toast"], out["undo"]) == ("Kept Copperline for 30 days.", True)
    assert (out["kept"], out["fact"]) == (True, "after")
    assert out["undoCall"] == ["post", "/api/watch/unkeep", {"media_item_id": "m1"}]
    assert out["afterUndo"] == "Keep undone."


def test_a_keep_the_server_says_cannot_be_undone_offers_no_undo() -> None:
    assert _run("keepNotToUndo") == [
        {"told": "Kept Copperline for 30 days.", "undo": False},
        {"told": "Kept Night Shift, Season 3 for 30 days.", "undo": False},
    ]


def test_keeping_an_unscheduled_movie_is_undone_by_removing_the_keep() -> None:
    out = _run("keepUnscheduled")
    assert out["calls"] == [
        ["postForm", "/api/media/m2/keep", {"duration": "forever", "read_action": "none"}],
        ["post", "/api/media/m2/unprotect", {"read": MADE}],
    ]
    assert out["toast"] == "Kept Saltmarsh forever."


def test_changing_an_existing_keep_offers_no_undo() -> None:
    out = _run("keepAgain")
    read = {f"read_{key}": value for key, value in MADE.items()}
    assert out["call"] == ["postForm", "/api/media/m3/keep", {"duration": "30 days", **read}]
    assert out["undo"] is False


def test_a_second_press_while_a_keep_runs_sends_nothing() -> None:
    assert _run("keepTwice") == {"calls": 1, "told": True}


def test_a_failed_keep_says_why_and_reads_nothing_again() -> None:
    out = _run("keepFails")
    assert out["errors"] == ["Couldn't save keep. Too many keep operations"]
    assert (out["reads"], out["toasts"]) == ([], 0)


def test_removing_a_keep_turns_the_card_back_in_place() -> None:
    out = _run("removeKeep")
    assert out["call"] == ["post", "/api/media/m3/unprotect", {"read": MADE}]
    assert out["toast"] == "Removed the keep on Glass Harbour."
    assert out["kept"] is False
    assert out["keys"] == ["id:m1", "id:m2", "show:77", "id:m3"]


def test_a_delete_is_confirmed_shows_busy_then_the_card_leaves_and_focus_moves_on() -> None:
    out = _run("deleteMovie")
    assert out["confirm"]["title"] == "Delete Copperline?"
    assert "22.1 GB" in out["confirm"]["body"]
    assert out["confirm"]["confirmVariant"] == "danger"
    assert out["busy"] == {"card": True, "label": "Deleting…", "spinner": True, "held": "true"}
    assert out["keys"] == ["id:m2", "show:77", "id:m3"]
    assert out["toast"] == "Deleted Copperline."
    assert out["focus"] is True
    assert out["fact"] == "after"


def test_a_refused_delete_puts_the_card_and_its_menu_back() -> None:
    out = _run("deleteRefused")
    assert (out["busy"], out["label"], out["held"], out["focus"]) == (False, "Movie", None, True)
    assert out["keys"] == ["id:m1", "id:m2", "show:77", "id:m3"]
    assert out["errors"] == ["Couldn't delete Copperline. This item is kept"]


def test_cancelling_the_confirm_deletes_nothing() -> None:
    # Focused by the script: a click does not focus a button in every browser.
    assert _run("deleteCancelled") == {"calls": [], "busy": False, "focus": True}


def test_a_season_deleted_from_the_show_dialog_updates_only_that_show_s_card() -> None:
    out = _run("seasonDelete")
    assert (out["opened"], out["deletes"], out["closed"]) == (True, 1, True)
    assert out["confirm"] == "Delete Night Shift — Season 1?"
    assert out["call"] == ["post", "/api/media/s1/delete", None]
    assert out["title"] == "Night Shift (1 season)"
    assert out["keys"] == ["id:m1", "id:m2", "show:77", "id:m3"]


def test_the_season_delete_list_can_be_read_again_and_stays_after_a_cancelled_delete() -> None:
    out = _run("seasonDeleteCancelledAndRetried")
    assert (out["failed"], out["retry"], out["focus"]) == (True, "Retry", True)
    failed, loading, inside, loaded = out["heard"]
    assert ("load" in failed, "Loading" in loading, inside, loaded) == (True, True, False, "")
    assert out["back"] == ["Delete", True]
    # A browser that does not focus what is clicked has somewhere to go back to.
    assert out["refocused"] is True
    assert out["cancelled"] == {"open": True, "deletes": 1, "posts": 0}


def test_the_season_delete_opens_at_its_heading_and_the_focus_never_leaves_it() -> None:
    title = "season-delete-title"
    assert _run("seasonDeleteOpens") == {
        "opened": title,
        "loaded": title,
        "tab": ["Delete", True],
        "shiftTab": "Close",
        "failed": "season-delete-retry",
        "retrying": title,
        "retried": "Delete",
    }


def test_the_keep_dialog_opens_as_the_show_it_is_for_at_the_size_it_will_have() -> None:
    out = _run("keepDialogOpens")
    assert out["title"] == "Keep Harbour Lights"
    assert (out["pressed"], out["focus"]) == (["Forever"], "Forever")
    assert out["waiting"] == {"rows": 5, "busy": "true"}
    assert out["loaded"] == {"rows": 0, "busy": None}
    # The width the page's scrollbar leaves is held only while it is open.
    assert out["bar"] == ["15px", ""]


def test_the_keep_dialog_of_a_title_page_holds_a_row_for_each_season_and_one_for_all() -> None:
    out = _run("keepDialogFromATitle")
    assert out["out"] == [1, 2, 3, 13]
    assert (out["missing"], out["none"]) == (3, 3)


def test_keeping_a_show_from_its_dialog_closes_it_and_updates_the_card_in_place() -> None:
    out = _run("showKeep")
    assert out["opened"] is True
    assert out["call"] == [
        "post",
        "/api/show/77/keep",
        {
            "duration": "30 days",
            "season_ids": ["s1"],
            "read": {"show": None, "seasons": {"s1": None}},
        },
    ]
    assert (out["closed"], out["kept"]) == (True, True)


def test_the_detail_can_keep_and_delete_with_a_plain_wrapper_and_no_grid_open() -> None:
    out = _run("detailCalls")
    assert out["keys"] == ["confirmDelete", "keep", "pickSeasonDelete", "removeKeep", "retireToast"]
    assert out["calls"] == [
        ["postForm", "/api/media/42/keep", {"duration": "7 days", "read_action": "none"}],
        ["post", "/api/media/42/delete", None],
    ]
    assert out["toasts"] == ["Kept Dune for 7 days.", "Deleted Dune."]
    assert out["deleted"] is True
    assert out["reads"] == []


def test_a_title_page_sends_back_the_keep_it_was_given() -> None:
    read = {f"read_{key}": value for key, value in MADE.items()}
    assert _run("detailKeepAsRead") == [
        ["postForm", "/api/media/42/keep", {"duration": "7 days", **read}],
        ["post", "/api/media/42/unprotect", {"read": MADE}],
    ]


def test_keeping_a_scheduled_movie_forever_offers_no_undo_that_would_lose_its_date() -> None:
    out = _run("keepForeverScheduled")
    assert out["call"] == [
        "postForm",
        "/api/media/m1/keep",
        {"duration": "forever", "read_action": "none"},
    ]
    assert (out["toast"], out["undo"]) == ("Kept Copperline forever.", False)


def test_remove_all_keeps_says_removing_on_its_own_button_with_no_note() -> None:
    out = _run("removeAllLabel")
    assert out["busy"] == {
        "label": "Removing…",
        "disabled": True,
        "note": "",
        "noteInButtons": False,
        "row": "Removing…",
    }
    assert out["after"] == "Remove all keeps"


def test_removing_a_keep_takes_away_the_undo_of_the_keep_it_removes() -> None:
    out = _run("removeKeepRetiresUndo")
    assert (out["before"], out["after"]) == (False, True)
    assert out["messages"] == ["Kept Copperline for 30 days.", "Removed the keep on Copperline."]
    # The retired Undo ran nothing: no unkeep request followed.
    assert out["calls"] == ["/api/watch/keep", "/api/media/m1/unprotect"]


def test_the_keep_dialog_opens_with_the_season_that_leaves_and_keeps_only_that_one() -> None:
    out = _run("keepLeavingSeason")
    assert out["ticked"] == ["s1", "s3"]
    assert out["sent"] == [[WATCH_KEEP, 30, "s3"]]
    assert out["told"] == ["Kept Night Shift, Season 3 for 30 days."]
    assert out["undone"] == [["/api/watch/unkeep", "", "s3"]]


def test_a_season_nobody_picked_is_never_ticked_or_sent() -> None:
    out = _run("keepNothingPicked")
    assert out["other"] == {"ticked": [], "sent": [], "errors": 1, "busy": False}
    assert out["kept"] == {"ticked": ["s1"], "sent": [], "errors": 1, "busy": False}


def test_the_keep_dialog_of_a_card_picks_the_season_whose_date_the_card_shows() -> None:
    out = _run("keepFromList")
    assert out["ticked"] == ["s1", "s2"]
    assert out["leavingOnes"] == [None, "3", "5"]
    assert out["nothingLeaves"] == []
    assert out["soonest"] == ["s3"]
    assert out["past"].endswith("leaves any day")


def test_the_keep_dialog_holds_the_room_of_keeps_only_for_a_show_that_has_one() -> None:
    assert _run("keepRoom") == {
        "kept": [True, True],
        "none": [False, False],
        "since": [False, True],
    }


def test_a_keep_changes_only_by_the_button_of_its_own_season() -> None:
    out = _run("keepChanged")
    assert out["only"] == {"s1": True, "s2": False, "all": False}
    assert (out["label"], out["pressed"]) == ("Set to 90 days", "true")
    assert out["sent"] == [["/api/media/s1/keep", "90 days", ""]]
    assert out["told"] == ["Kept Night Shift, Season 1 for 90 days."]
    assert out["undo"] is False
    # Unticking takes the change back with it; ticking again keeps the keep there was.
    assert out["afterUntick"] == [[WATCH_KEEP, 30, "s3"]]


def test_an_unticked_season_loses_its_keep_and_the_toast_names_it() -> None:
    out = _run("keepRemoved")
    assert out["sent"] == [[WATCH_KEEP, 30, "s3"], ["/api/media/s1/unprotect", "", ""]]
    assert out["told"] == ["Kept Night Shift, Season 3 for 30 days. Removed the keep on Season 1."]
    assert out["undo"] is False
    assert out["only"] == [
        [["/api/media/s1/unprotect", "", ""]],
        ["Removed the keep on Night Shift, Season 1."],
    ]


def test_keeping_the_whole_show_leaves_the_keeps_its_seasons_have() -> None:
    out = _run("keepWholeShow")
    assert out["ticked"] == ["all-seasons", "s1", "s2", "s3"]
    assert out["sent"] == [["/api/show/77/keep", "30 days", ["s2", "s3"]]]
    assert out["told"] == ["Kept Night Shift, Season 2, Season 3 and new seasons for 30 days."]
    assert out["allKept"] == [[], 1]


def test_unticking_a_season_of_a_kept_show_is_one_change_on_the_server() -> None:
    out = _run("keepShowUnticked")
    assert out["ticked"] == ["all-seasons", "s1", "s2", "s3"]
    sent, told = out["forever"]
    assert [call[0] for call in sent] == ["/api/show/77/remove"]
    assert told == ["Removed the keep on Night Shift, Season 3."]
    assert [call[0] for call in out["days"]] == ["/api/show/77/remove"]


def test_a_show_keep_changes_only_by_its_own_button_and_spares_a_longer_season_keep() -> None:
    out = _run("keepShowChanged")
    assert out["untouched"] == [[], 1]
    assert out["seasonSet"] is False
    assert out["sent"] == [["/api/show/77/keep", "30 days", ["s2", "s3"]]]


def test_after_a_failure_the_dialog_offers_only_what_was_not_saved() -> None:
    out = _run("keepPartFails")
    assert len(out["errors"]) == 1
    assert "Season 2" in out["errors"][0]
    assert "Season 3" not in out["errors"][0]
    assert out["ticked"] == ["s1", "s2", "s3"]
    assert (out["open"], out["focus"]) == (True, "keep-dialog-confirm")
    assert out["again"] == [[WATCH_KEEP, 30, "s2"]]


def test_a_season_keep_is_undone_by_what_made_it_and_not_once_it_was_removed() -> None:
    out = _run("keepUndoRetired")
    assert out["dismissed"] is True
    assert out["sent"] == [[WATCH_KEEP, 30, "s3"], ["/api/media/s3/unprotect", "", ""]]
    assert out["forever"] == [[["/api/media/s3/keep", "forever", ""]], False]
    assert out["unscheduled"] == [
        ["/api/media/s2/keep", "forever", ""],
        ["/api/media/s2/unprotect", "", ""],
    ]


def test_every_change_in_the_keep_dialog_says_which_keep_it_read() -> None:
    out = _run("keepSaysWhatItRead")
    forever, five, twelve = (out["expected"][name] for name in ("forever", "five", "twelve"))
    assert out["seasons"] == [
        [
            "/api/media/s1/keep",
            {
                "duration": "30 days",
                "read_action": "protected_forever",
                "read_execute_at": "",
                "read_kept_at": forever["kept_at"],
            },
        ],
        ["/api/media/s3/keep", {"duration": "30 days", "read_action": "none"}],
        ["/api/media/s2/unprotect", {"read": five}],
    ]
    assert out["all"] == [
        [
            "/api/show/77/keep",
            {
                "duration": "30 days",
                "season_ids": ["s2"],
                "read": {"show": None, "seasons": {"s1": forever, "s2": None}},
            },
        ]
    ]
    read = {"show": twelve, "seasons": {"s1": twelve, "s2": None}}
    assert out["end"] == [["/api/show/77/remove", {"read": read, "drops": ["s2"]}]]
    assert out["removeAll"] == [["/api/show/77/remove", {"read": read, "drops": ["s1", "s2"]}]]
    assert out["removeOwn"] == [["/api/media/s1/unprotect", {"read": five}]]


def test_the_keep_dialog_shows_a_keep_that_changed_while_it_was_open_and_sends_no_more() -> None:
    out = _run("keepChangedMeanwhile")
    for name, ticked in (
        ("season", ["s1"]),
        ("all", ["s1"]),
        ("end", ["all-seasons", "s1", "s2", "s3"]),
        ("drop", ["s1"]),
        ("removeAll", ["all-seasons", "s1", "s2", "s3"]),
    ):
        case = out[name]
        assert len(case["errors"]) == 1 and OPEN_CHANGED in case["errors"][0], name
        # Read again: what is kept now, and nothing else ticked.
        assert (case["open"], case["gets"], case["ticked"]) == (True, 2, ticked), name
        assert (case["phases"], case["told"]) == (["busy", "done"], []), name
    assert [out[n]["sent"] for n in ("season", "all", "end", "drop")] == [1, 1, 1, 1]
    assert out["all"]["focus"] == "keep-dialog-confirm"
    for status in ("watch404", "watch409"):
        assert OPEN_CHANGED in out[status]["errors"][0]
        assert out[status]["phases"] == ["busy", "done"]
        assert out[status]["ticked"] == ["s1"]


def test_a_film_whose_keep_changed_meanwhile_is_read_again_and_its_undo_names_its_own_keep() -> (
    None
):
    out = _run("filmKeepChangedMeanwhile")
    assert out["phases"] == ["busy", "done"] * 3
    assert (out["errors"], out["toasts"]) == (2, 1)
    assert out["undo"] == ["post", "/api/media/m2/unprotect", {"read": MADE}]


def test_a_dialog_s_status_line_is_there_empty_before_it_says_anything() -> None:
    loading = ["status", "", "Loading the seasons…"]
    assert _run("dialogsSayAfter") == [loading, loading, ""]


def test_a_title_that_could_not_be_read_again_holds_the_keep_that_was_saved() -> None:
    out = _run("unreadRowHoldsItsKeep")
    assert (out["kept"], out["removed"], out["undone"], out["scheduled"]) == (
        MADE,
        None,
        None,
        MADE,
    )
    read = {f"read_{key}": value for key, value in MADE.items()}
    assert out["next"] == ["postForm", "/api/media/m1/keep", {"duration": "forever", **read}]


def test_an_undo_that_came_too_late_reads_its_title_again() -> None:
    assert _run("undoTooLate") == {"film": ["busy", "done"], "season": ["busy", "done"]}


def test_a_card_whose_keep_changed_meanwhile_is_left_alone_read_again_and_said_so() -> None:
    out = _run("staleRow")
    for name in ("keep", "remove", "scheduled"):
        case = out[name]
        assert [phase for phase, _ in case["phases"]] == ["busy", "done"], name
        assert (case["toasts"], case["busy"], case["kept"]) == (0, False, True), name
        assert case["read"] == out["forever"], name
        assert len(case["errors"]) == 1 and "changed meanwhile" in case["errors"][0], name
    assert out["remove"]["call"] == ["post", "/api/media/m3/unprotect", {"read": MADE}]
    assert out["keptWithNoKeep"]["call"][2] == {"read": {"action": "none"}}
    # Read again in vain: nothing was saved, so it does not read as kept.
    assert (out["unread"]["busy"], out["unread"]["kept"]) == (False, False)


def test_the_undo_of_a_season_takes_back_only_the_keep_its_save_made() -> None:
    out = _run("seasonUndoChangedMeanwhile")
    assert out["undo"] == ["/api/media/s2/unprotect", {"read": MADE}]
    assert len(out["errors"]) == 1 and "Season 2" in out["errors"][0]
    assert out["phases"] == ["busy", "done", "busy", "done"]
    assert out["told"] == ["Kept Night Shift, Season 2 for 30 days."]
    # The dialog is closed: nothing is read for it.
    assert out["reads"] == 1
    # With no word of the keep made there is nothing safe to undo.
    assert out["unsaid"] is False


def test_a_save_takes_away_the_undos_it_affects_as_it_starts() -> None:
    assert _run("undoRetiredAsASaveStarts") == {
        "same": True,
        "other": False,
        "all": True,
        "show": True,
        "removeAll": True,
        "film": True,
        "filmKeptAgain": True,
    }


def test_an_undo_holds_the_open_dialog_and_the_seasons_are_read_again_after_it() -> None:
    assert _run("undoWithTheDialogOpen") == {
        "before": [False, False],
        "ticked": ["s1"],
        "during": [True, True],
        "after": [False, False],
        "reads": 1,
        "tickedAfter": [],
    }


def test_an_undo_waits_for_a_save_of_the_same_show() -> None:
    out = _run("undoWaitsForASave")
    assert out["during"] == ["/api/media/s1/keep", "/api/media/s2/keep"]
    assert out["after"] == ["/api/media/s1/unprotect"]


def test_remove_all_waits_for_an_undo_of_the_same_show() -> None:
    assert _run("removeAllWaitsForAnUndo") == {"during": 2, "after": 4}


def test_a_save_that_fails_removes_no_keep() -> None:
    out = _run("failedKeepDropsNothing")
    assert (out["sent"], out["phases"]) == (["/api/media/s2/keep"], ["busy", "failed"])
    assert (out["show"], out["showPhases"]) == (["/api/show/77/remove"], ["busy", "failed"])
    # The dialog is as it was, to try again.
    assert (out["label"], out["ticked"], out["disabled"]) == ("Update", ["s1", "s2"], False)


def test_what_was_saved_is_announced_though_the_rest_failed() -> None:
    out = _run("partSavedIsAnnounced")
    for name, phase, season in (
        ("kept", "done", "Season 2"),
        ("removed", "done", "Season 2"),
        ("nothing", "failed", "Season 1"),
    ):
        assert out[name]["phases"] == ["busy", phase], name
        # The failure is said and nothing is told as saved.
        assert len(out[name]["errors"]) == 1 and season in out[name]["errors"][0], name
        assert out[name]["told"] == [], name


def test_setting_all_seasons_leaves_a_longer_keep_that_is_a_seasons_own() -> None:
    out = _run("keepShowShortened")
    assert out["forever"] == [["/api/show/77/keep", "7 days", ["s1", "s4", "s5"]]]
    assert out["days"] == [["/api/show/77/keep", "7 days", ["s1", "s3"]]]
    sent, errors = out["own"]
    assert (sent, len(errors)) == ([], 1)


def test_ctrl_z_reaches_the_undo_of_an_admin_keep_made_in_details() -> None:
    assert _run("keepToastDialog") == ["ttl", None]


def test_an_undo_is_announced_as_it_is_pressed_though_it_waits_for_a_save() -> None:
    assert _run("undoAnnouncedAtOnce") == ["busy"]


def test_a_dialog_button_clicked_without_the_focus_keeps_the_focus_in_the_dialog() -> None:
    assert _run("clickedUnfocused") == {
        "keep": "keep-season-list",
        "asked": "keep-season-list",
        "cancelled": "keep-dialog-remove",
    }


def test_a_change_in_a_show_s_dialog_names_the_seasons_it_changes_as_it_starts() -> None:
    both = ["s1", "s2"]
    assert _run("seasonsAnnounced") == {"some": [["s2"], ["s2"]], "show": [both], "end": [both]}


def test_only_the_last_keep_change_of_a_title_has_a_toast() -> None:
    assert _run("oneKeepToast") == [
        ["Kept Copperline for 7 days."],
        ["Removed the keep on Copperline."],
        ["Kept Copperline for 90 days."],
        ["Kept Copperline forever."],
        ["Kept Copperline forever.", "Kept Other forever."],
    ]


def test_a_delete_refused_as_kept_shows_the_keep_on_its_card() -> None:
    out = _run("deleteRefusedAsKept")
    assert out["kept"] == {"reads": 1, "kept": True, "busy": False, "errors": 1}
    assert out["delete_refused"] == {"reads": 0, "kept": False, "busy": False, "errors": 1}


BY_SIZE = ["push", "/watch?view=list&type=movie&sort=size_desc"]


def test_a_keep_says_it_is_kept_holds_its_menu_and_a_failure_puts_both_back() -> None:
    at_rest = {"busy": False, "label": "Movie", "held": None}
    assert _run("keepBusyThenFails") == {
        "busy": {"busy": True, "label": "Keeping…", "held": "true"},
        "after": at_rest,
        "errors": ["Couldn't save keep. Too many keep operations"],
        "focus": True,
    }


def test_a_saved_keep_stays_busy_until_the_title_as_it_is_now_is_in() -> None:
    out = _run("keepBusyUntilItIsBack")
    assert out["whileReading"]["busy"] is True
    assert (out["busy"], out["kept"], out["focus"]) == (False, True, True)


def test_a_saved_keep_that_could_not_be_read_again_shows_as_kept_with_no_delete() -> None:
    out = _run("keepThenReadFails")
    assert out["errors"] == ["Couldn’t read the title again. Reload to see the change."]
    assert (out["busy"], out["held"], out["kept"]) == (False, None, True)
    assert (out["del"], out["unkeep"]) == (None, "item")


def test_a_show_whose_seasons_were_kept_and_could_not_be_read_is_not_said_to_be_kept() -> None:
    assert _run("showKeepThenReadFails") == {"kept": False, "del": {"show_rk": "77"}, "busy": False}


def test_undo_and_remove_keep_say_so_and_a_failure_puts_the_card_back() -> None:
    out = _run("removingIsShown")
    going = {"busy": True, "label": "Removing…", "held": "true"}
    rest = {"busy": False, "label": "Movie", "held": None}
    assert [out["undo"], out["remove"]] == [going, going]
    assert [out["afterUndo"], out["afterRemove"]] == [rest, rest]
    assert len(out["errors"]) == 2


def test_after_a_season_delete_the_focus_is_on_its_show_or_on_the_next_card() -> None:
    out = _run("seasonDeleteFocus")
    assert (out["stayed"]["focus"], out["left"]["focus"]) == (True, True)
    assert out["left"]["keys"] == ["id:m1", "id:m2", "id:m3"]


def test_a_kept_title_that_leaves_the_filter_stays_in_place_for_its_undo() -> None:
    out = _run("keptLeavesTheFilter")
    assert out["keys"] == ["id:m1", "id:m2", "show:77", "id:m3"]
    assert (out["kept"], out["undo"], out["fact"]) == (True, True, "after")
    assert out["read"].endswith("&type=stale&only=id%3Am1")


def test_a_card_stays_busy_until_its_last_action_ends_though_the_grid_is_filled_anew() -> None:
    assert _run("busyThroughAnother") == {
        "oneLeft": True,
        "noneLeft": False,
        "unkeepLeft": "Removing…",
        "bothFailed": {"busy": False, "label": "Movie", "held": None},
        "unreadStillBusy": True,
        "filledAnew": True,
    }


def test_a_page_read_before_a_change_has_the_title_read_again() -> None:
    assert _run("pageFromBeforeAChange") == ["/api/watch/library/library/row?limit=60&only=id%3Am1"]


def test_a_first_page_read_before_a_change_has_the_title_read_again() -> None:
    again = ["/api/watch/library/library/row?limit=60&only=id%3Am1"]
    assert _run("firstPageFromBeforeAChange") == {
        "sorted": again,
        "heldBack": again,
        "unchanged": ["/api/watch/library/library/row?limit=60&sort=size"],
        "opened": again,
    }


def test_a_schedule_read_before_a_change_leaves_the_changed_title_as_it_is() -> None:
    assert _run("scheduledBeforeAChange") == [True, True]


def test_what_was_read_before_a_schedule_is_read_again_once_and_shows_it() -> None:
    walk, only = "?limit=60", "?limit=60&only=id%3Am2"
    assert _run("readBeforeASchedule") == {
        "sorted": {"reads": [walk], "kept": True},
        "gone": {"reads": [walk, only], "kept": None},
        "page": {"reads": [walk], "kept": True},
        "opened": {"reads": [walk], "kept": True},
        "alone": {"reads": [only, walk, only], "kept": True, "busy": False},
        "twice": {"reads": 2, "kept": True, "fact": "new"},
    }


def test_an_older_read_of_a_title_that_lands_after_a_newer_one_says_nothing() -> None:
    assert _run("readsOutOfOrder") == [[True, "second"], [True, "second"]]


def test_delete_of_a_title_kept_while_its_menu_was_open_says_so_and_deletes_nothing() -> None:
    out = _run("deleteOfOneKeptMeanwhile")
    assert (out["calls"], out["confirms"]) == ([], 0)
    (told,) = out["told"]
    assert "Glass Harbour" in told
    assert "kept" in told


def test_a_change_made_while_the_grid_is_closed_reads_nothing() -> None:
    assert _run("doneOutOfSight") == {"reads": [], "errors": []}


def test_an_undo_takes_the_focus_to_its_card_and_from_nothing_that_has_it() -> None:
    assert _run("undoFocus") == {
        "plain": "card",
        "typing": "watch-search",
        "details": "Undo",
        "dialog": "Undo",
    }


def test_a_change_of_what_is_scheduled_brings_the_cards_up_to_date_where_they_are() -> None:
    out = _run("scheduledChanged")
    assert out["subs"] == ["now m1", "now m2", "now s1", "now m3"]
    assert (out["same"], out["focus"], out["fact"], out["closed"]) == (True, True, "fresh", 0)


def test_the_menu_offers_an_admin_what_there_is_to_do_of_each_title() -> None:
    out = _run("menus")
    facts = ["Admins only", "22.1 GB"]
    assert out["film"] == out["show"] == [*facts, "Keep…", "Delete…"]
    assert out["keptFilm"] == [*facts, "Change keep…", "Remove keep"]
    # A show's keeps can sit on several seasons: its dialog has them.
    assert out["keptShow"] == [*facts, "Change keep…", "Remove keep…"]
    assert out["none"] == []
    assert [d for d, _ in out["lengths"][1]] == ["7 days", "30 days", "90 days", "forever"]
    assert (out["lengths"][0], out["notThere"]) == (True, False)


def test_removing_the_keeps_of_a_show_opens_its_dialog_and_sends_nothing_yet() -> None:
    assert _run("removeKeepOfAShow") == {"dialog": True, "posts": 0}


def test_a_title_watch_cannot_open_has_no_link_and_its_menu() -> None:
    assert _run("unheldCard") == {"href": None, "more": True, "key": "id:x1"}


def test_an_empty_grid_says_what_the_server_says_of_it() -> None:
    assert _run("emptyFilters") == {
        "chosen": {"title": "Nothing is kept", "filter": ""},
        "loaded": "Nothing is kept",
    }


def test_going_to_it_changes_nothing_until_it_is_in_or_slow_and_can_be_called_off() -> None:
    out = _run("going")
    arrived = ["size", "movies", True, 4]
    waiting = ["size", "movies", True, None]
    assert all(one["before"] == 0 and len(one["read"]) == 1 for one in out.values())
    assert {name: (one["told"], one["opened"]) for name, one in out.items()} == {
        "whenIn": (["leave", "arrive"], [arrived]),
        "slow": (["leave"], [waiting]),
        "fails": (["fail"], []),
        "slowThenFails": (["leave"], [waiting]),
        "calledOff": ([], []),
        "fromItself": (["leave"], [arrived]),
    }


def test_it_is_gone_to_at_once_where_asked_again() -> None:
    assert _run("shown") == [["size", "tv", True, None]]


def test_a_mark_of_one_s_own_changes_the_counts_and_leaves_the_cards() -> None:
    assert _run("marked") == {"fact": "counted", "sub": "Movie"}


def test_how_many_titles_there_are_follows_what_is_deleted_and_scanned_wherever_it_is_said() -> (
    None
):
    out = _run("totals")
    assert out["open"] == ["All 3 titles", False]
    assert out["opens"] == [3, None]
    assert out["one"] == ["All 1 title", False]
    assert out["none"][1] is True
    assert out["scanned"] == ["All 4 titles", False]
    assert out["late"] == ["All 2 titles", False]
    assert out["heard"] == [1, 0, 4]
