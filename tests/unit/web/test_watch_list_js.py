"""The Watch list view and the admin actions, run under node against a fake DOM.

Nothing here reloads the page: every action ends with the list changed in place.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_list_harness.js"


# The keep the server answers a save made.
MADE = {
    "action": "snoozed",
    "execute_at": "2026-12-24T12:00:00+00:00",
    "kept_at": "2026-11-24T12:00:00+00:00",
}
OPEN_CHANGED = "changed while this was open"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_keeping_a_scheduled_movie_turns_its_pill_in_place_and_undo_brings_the_date_back() -> None:
    out = _run("keepScheduled")
    assert out["call"] == ["post", "/api/watch/keep", {"media_item_id": "m1", "days": 30}]
    assert (out["toast"], out["undo"]) == ("Kept Copperline for 30 days.", True)
    assert "keep-trigger--kept" in out["pill"]
    assert "is-just-kept" in out["pill"]
    assert out["summary"] == "summary after"
    assert out["history"] == []
    assert out["undoCall"] == ["post", "/api/watch/unkeep", {"media_item_id": "m1"}]
    assert out["afterUndo"] == "Keep undone."


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
    assert _run("keepTwice") == {"calls": 1}


def test_a_failed_keep_says_why_and_reads_nothing_again() -> None:
    out = _run("keepFails")
    assert out["errors"] == ["Couldn't save keep. Too many keep operations"]
    assert (out["fetches"], out["toasts"]) == ([], 0)


def test_removing_a_keep_turns_the_row_back_in_place() -> None:
    out = _run("removeKeep")
    assert out["call"] == ["post", "/api/media/m3/unprotect", {"read": MADE}]
    assert out["toast"] == "Removed the keep on Glass Harbour."
    assert out["kept"] is False
    assert out["keys"] == ["id:m1", "id:m2", "show:77", "id:m3"]


def test_a_delete_is_confirmed_shows_busy_then_the_row_leaves_and_focus_moves_on() -> None:
    out = _run("deleteMovie")
    assert out["confirm"]["title"] == "Delete Copperline?"
    assert "22.1 GB" in out["confirm"]["body"]
    assert out["confirm"]["confirmVariant"] == "danger"
    assert out["busy"] == {"row": True, "label": "Deleting…", "spinner": True, "disabled": True}
    assert out["keys"] == ["id:m2", "show:77", "id:m3"]
    assert out["toast"] == "Deleted Copperline."
    assert out["focus"] is True
    assert out["range"] == "range after"
    assert out["history"] == []


def test_a_refused_delete_puts_the_row_and_its_button_back() -> None:
    out = _run("deleteRefused")
    assert (out["busy"], out["label"], out["disabled"]) == (False, "Delete", False)
    assert out["keys"] == ["id:m1", "id:m2", "show:77", "id:m3"]
    assert out["errors"] == ["Couldn't delete Copperline. This item is kept"]


def test_cancelling_the_confirm_deletes_nothing() -> None:
    assert _run("deleteCancelled") == {"calls": [], "busy": False}


def test_a_season_deleted_from_the_show_dialog_updates_only_that_show_row() -> None:
    out = _run("seasonDelete")
    assert (out["opened"], out["deletes"], out["closed"]) == (True, 1, True)
    assert out["confirm"] == "Delete Night Shift — Season 1?"
    assert out["call"] == ["post", "/api/media/s1/delete", None]
    assert out["title"] == "Night Shift (1 season)"
    assert out["keys"] == ["id:m1", "id:m2", "show:77", "id:m3"]


def test_keeping_a_show_from_its_dialog_closes_it_and_updates_the_row_in_place() -> None:
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
    assert out["history"] == []


def test_the_detail_can_keep_and_delete_with_a_plain_wrapper_and_no_list_on_the_page() -> None:
    out = _run("detailCalls")
    assert out["keys"] == ["confirmDelete", "keep", "pickSeasonDelete", "removeKeep"]
    assert out["calls"] == [
        ["postForm", "/api/media/42/keep", {"duration": "7 days", "read_action": "none"}],
        ["post", "/api/media/42/delete", None],
    ]
    assert out["toasts"] == ["Kept Dune for 7 days.", "Deleted Dune."]
    assert out["deleted"] is True
    assert out["fetches"] == []


def test_the_switch_swaps_to_the_list_in_place_and_back_and_forward_follow() -> None:
    out = _run("switchView")
    assert out["prevented"] is True
    assert out["fetches"] == ["/watch/list?q=harbour"]
    assert out["history"] == [["push", "/watch?view=list&q=harbour"]]
    assert (out["listShown"], out["browseHidden"]) == (True, True)
    assert out["current"] == "page"
    assert out["placeholder"] == "Search your library"
    assert out["rows"] == 4
    assert out["events"] == [["view", "list", True]]
    assert out["back"] == {
        "listHidden": True,
        "browseShown": True,
        "events": [["view", "rows", True]],
    }
    assert out["forward"] == {"listShown": True, "fetches": 1}


def test_a_modified_click_on_the_switch_is_left_to_the_browser() -> None:
    assert _run("switchModified") == {"prevented": False, "fetches": []}


def test_the_search_field_filters_the_list_and_a_late_older_reply_is_dropped() -> None:
    out = _run("search")
    assert out["fetches"] == ["/watch/list?q=har", "/watch/list?q=harbour"]
    # The address follows only the list that is shown: the dropped reply never moves it.
    assert out["history"] == [["replace", "/watch?view=list&q=harbour"]]
    assert out["keys"] == ["id:m2"]
    assert out["summary"] == "summary harbour"


def test_pills_pages_and_clear_change_the_list_in_place_with_history() -> None:
    out = _run("navigate")
    assert out["afterPill"] == ["id:m3"]
    assert out["focus"] == "kept"
    assert out["afterPage"] == ["show:77", "id:m3"]
    assert out["cleared"] == ""
    assert out["fetches"] == ["/watch/list?type=kept", "/watch/list?page=2", "/watch/list"]
    assert out["history"] == [
        ["push", "/watch?view=list&type=kept"],
        ["push", "/watch?view=list&page=2"],
        ["push", "/watch?view=list"],
    ]


def test_the_sort_select_reorders_from_the_first_page_and_keeps_focus() -> None:
    out = _run("sort")
    assert out["fetches"] == ["/watch/list?sort=deletion_asc"]
    assert out["history"] == [["push", "/watch?view=list&sort=deletion_asc"]]
    assert out["focus"] == "watch-list-sort"


def test_a_keep_shows_keeping_on_the_row_and_a_failure_puts_the_row_and_focus_back() -> None:
    out = _run("keepBusyThenFails")
    assert out["busy"] == {
        "row": True,
        "label": "Keeping…",
        "ariaBusy": "true",
        "disabled": True,
        "deleteDisabled": True,
    }
    assert out["after"] == {
        "row": False,
        "label": "Keep",
        "ariaBusy": None,
        "disabled": False,
        "deleteDisabled": False,
    }
    assert out["errors"] == ["Couldn't save keep. Too many keep operations"]
    assert out["focus"] is True


def test_a_saved_keep_stays_busy_until_the_changed_row_is_in_and_focus_lands_on_it() -> None:
    out = _run("keepBusyUntilTheRowIsBack")
    assert out["whileReading"] == {"busy": True, "toast": "Kept Copperline for 30 days."}
    assert (out["busy"], out["kept"], out["focus"]) == (False, True, True)


def test_undo_and_remove_keep_show_removing_and_a_failure_puts_the_row_back() -> None:
    out = _run("removingIsShown")
    assert out["undo"] == {"label": "Removing…", "busy": True}
    assert out["afterUndoFail"] == {"busy": False, "label": "Keep"}
    assert out["remove"] == {"label": "Removing…", "busy": True}
    assert out["afterRemoveFail"] == {"busy": False, "label": "Kept"}
    assert out["errors"] == [
        "Couldn't undo the keep. No active protection found",
        "Couldn't remove keep. No active protection found",
    ]


def test_a_refused_delete_puts_focus_back_on_its_delete() -> None:
    assert _run("deleteRefusedFocus") == {"blurred": True, "focus": True}


def test_after_a_season_delete_focus_is_on_the_show_delete_or_the_next_row() -> None:
    out = _run("seasonDeleteFocus")
    assert out["stayed"]["onShowDelete"] is True
    assert out["left"]["keys"] == ["id:m1", "id:m2", "id:m3"]
    assert out["left"]["onNextRow"] is True


def test_keeping_a_scheduled_movie_forever_offers_no_undo_that_would_lose_its_date() -> None:
    out = _run("keepForeverScheduled")
    assert out["call"] == [
        "postForm",
        "/api/media/m1/keep",
        {"duration": "forever", "read_action": "none"},
    ]
    assert (out["toast"], out["undo"]) == ("Kept Copperline forever.", False)


def test_a_kept_row_that_leaves_the_page_stays_in_place_in_its_new_state() -> None:
    out = _run("keptRowLeavesThePage")
    assert out["keys"] == ["id:m1", "id:m2", "show:77", "id:m3"]
    assert (out["kept"], out["busy"], out["undo"]) == (True, False, True)
    assert out["summary"] == "summary after"


def test_a_deleted_movie_row_is_replaced_by_the_next_page_first_row() -> None:
    out = _run("deleteRefills")
    assert out["keys"] == ["id:m2", "show:77", "id:m3", "id:m9"]
    assert out["range"] == "range after"


def test_a_sort_that_fails_to_load_puts_the_select_back_and_leaves_the_address() -> None:
    out = _run("sortFails")
    assert (out["select"], out["history"], out["busy"]) == ("added_desc", [], None)
    assert out["errors"] == ["Couldn't load the list. Try again."]


def test_a_filter_that_fails_to_load_leaves_the_address_and_the_rows() -> None:
    out = _run("pillFails")
    assert out["history"] == []
    assert out["keys"] == ["id:m1", "id:m2", "show:77", "id:m3"]


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


def test_a_saved_keep_whose_list_refresh_fails_frees_the_row() -> None:
    out = _run("keepThenRefreshFails")
    assert out["errors"] == ["Couldn't refresh the list. Reload to see the change."]
    assert (out["busy"], out["triggerDisabled"]) == (False, False)
    # The keep was saved, so the row shows it as the server would: kept, with no Delete.
    assert (out["label"].strip(), out["triggerKept"], out["rowKept"]) == ("Kept", True, True)
    assert out["ariaLabel"] == "Manage keep on Copperline"
    assert (out["wrapperKept"], out["deletion"], out["hasDelete"]) == ("true", "Kept", False)
    assert out["focus"] is True


def test_quick_filter_clicks_with_motion_end_with_one_table_for_the_last_choice() -> None:
    out = _run("rapidFilters")
    assert out["during"] == [1, 1, 1, 1, 1]
    assert (out["blocks"], out["stacks"], out["inert"]) == (1, 0, 0)
    assert out["keys"] == ["show:77"]
    assert (out["on"], out["type"]) == (["tv"], "tv")
    assert out["address"] == ["push", "/watch?view=list&type=tv"]


def test_a_search_or_sort_asked_while_a_fetch_runs_builds_on_what_was_asked() -> None:
    out = _run("askedWhileLoading")
    # Typed then cleared before the reply: the reply for the typed text is dropped.
    assert out["searchBack"]["summary"] == "summary all"
    assert len(out["searchBack"]["keys"]) == 4
    assert out["searchBack"]["address"] == ["replace", "/watch?view=list"]
    # A sort picked while a filter loads keeps that filter.
    assert out["sortAfterPill"]["fetches"][-1] == "/watch/list?type=kept&sort=deletion_asc"
    assert out["sortAfterPill"]["summary"] == "summary kept by date"
    assert out["sortAfterPill"]["address"] == [
        "push",
        "/watch?view=list&type=kept&sort=deletion_asc",
    ]


def test_a_list_left_while_a_filter_loads_leaves_the_address_to_the_rows() -> None:
    out = _run("leftWhileLoading")
    assert out["history"] == [["push", "/watch"]]
    assert out["left"] == {"list": False, "browse": True}
    assert (out["back"], out["keys"]) == ({"list": True, "browse": False}, 4)


def test_the_view_that_comes_is_there_while_the_one_that_goes_fades_out() -> None:
    listed = ["hidden false ", "shown false "]
    assert _run("overlappingViews") == {
        "during": ["shown true absolute", "shown false "],
        "after": listed,
        "back": ["shown true absolute", "shown false "],
        "end": listed,
    }


def test_the_list_is_out_of_the_way_before_the_search_shows_its_results() -> None:
    out = _run("listToResults")
    # It neither pushes the results down nor is moved by the page going to its top.
    assert out["asResultsCame"] == "shown true absolute"
    assert out["top"] == "-300px"
    assert out["results"] == {"shown": True, "fadesIn": True}
    assert (out["end"], out["browse"]) == ("hidden false ", False)


def test_the_list_stays_where_it_was_on_screen_as_the_page_it_leaves_gets_short() -> None:
    assert _run("listLeftOnAShortPage") == {"top": "0px", "y": 300, "padding": ""}


def test_rows_a_category_page_s_end_brings_back_do_not_flash_under_the_list() -> None:
    assert _run("categoryToList") == {"browse": False, "list": True, "faded": 0}


def test_back_from_the_list_to_a_grid_leaves_the_rows_hidden() -> None:
    assert _run("backToGrid") == {"opened": ["movies"], "browse": False, "list": False}


def test_a_header_pill_or_page_link_of_the_list_showing_builds_on_the_list_loading() -> None:
    out = _run("linksWhileLoading")
    # A header sorts the filter that is loading, from its first page.
    assert out["header"]["fetches"][-1] == "/watch/list?type=movie&sort=name_asc"
    assert out["header"]["summary"] == "summary movies by name"
    assert out["header"]["history"] == [["push", "/watch?view=list&type=movie&sort=name_asc"]]
    # A pill filters the search that is loading.
    assert out["pill"]["summary"] == "summary kept har"
    assert out["pill"]["history"] == [["push", "/watch?view=list&q=har&type=kept"]]
    # A page of the old list is no page of the filter that is loading.
    assert out["page"]["fetches"] == ["/watch/list?type=movie"]
    assert out["page"]["summary"] == "summary movies"
    assert out["page"]["history"] == [["push", "/watch?view=list&type=movie"]]
    # While another page of the same list loads, the page pressed last wins.
    assert out["pages"]["summary"] == "summary all 2"
    assert out["pages"]["history"] == [["push", "/watch?view=list&page=2"]]
    # A new tab is the browser's own, on the link's address.
    assert out["modified"] == {"newTab": [False, False], "fetches": ["/watch/list?type=movie"]}


def test_the_last_row_leaving_while_a_filter_loads_does_not_replace_that_filter() -> None:
    out = _run("lastRowLeavesWhileLoading")
    assert (out["keys"], out["summary"]) == (["id:m3"], "summary kept")
    assert out["history"] == [["push", "/watch?view=list&type=kept"]]


def test_focus_after_a_sort_header_lands_on_the_rows_coming_in() -> None:
    assert _run("sortHeaderFocus") == {"header": True, "inert": False, "old": False}


def test_removing_a_keep_takes_away_the_undo_of_the_keep_it_removes() -> None:
    out = _run("removeKeepRetiresUndo")
    assert (out["before"], out["after"]) == (False, True)
    assert out["messages"] == ["Kept Copperline for 30 days.", "Removed the keep on Copperline."]
    # The retired Undo ran nothing: no unkeep request followed.
    assert out["calls"] == ["/api/watch/keep", "/api/media/m1/unprotect"]


def test_a_change_of_what_is_scheduled_brings_the_list_up_to_date_and_keeps_the_focus() -> None:
    assert _run("scheduledChanged") == {
        "focus": {"key": "id:m1", "keep": True, "fresh": True},
        # its row is not in the list any more
        "gone": "id:m2",
        "first": "id:m2",
        "summary": "summary without it",
        "fetches": ["/watch/list", "/watch/list"],
    }


def test_a_change_of_what_is_scheduled_waits_for_a_list_that_loads_or_is_hidden() -> None:
    assert _run("scheduledChangedMeanwhile") == {
        "during": 0,
        "after": ["/watch/list?type=movie"] * 2,
        "hidden": 1,
        "shown": 2,
    }


WATCH_KEEP = "/api/watch/keep"


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


def test_the_keep_dialog_of_a_list_row_picks_the_season_whose_date_the_row_shows() -> None:
    out = _run("keepFromList")
    assert out["ticked"] == ["s1", "s2"]
    assert out["leavingOnes"] == [None, "3", "5"]
    assert out["nothingLeaves"] == []
    assert out["soonest"] == ["s3"]


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


def test_the_arrow_pressed_keeps_the_focus_and_at_the_last_page_the_other_one_has_it() -> None:
    assert _run("pageArrows") == [
        ["2", "Next page", True],
        ["3", "Previous page", True],
        ["2", "Previous page", True],
    ]


def test_an_action_that_fails_while_the_rows_show_lets_go_of_its_row() -> None:
    assert _run("failsOutOfSight") == {
        "before": True,
        "busy": False,
        "label": "Keep",
        "disabled": False,
    }


def test_a_row_stays_busy_through_a_sort_until_its_last_action_ends() -> None:
    out = _run("busyThroughASort")
    assert (out["during"], out["after"]) == ([True, True], [False, False])
    assert (out["oneLeft"], out["noneLeft"]) == (True, False)
    # The list that was on its way when the keep was saved is read again.
    assert out["lateListReads"] == 2


def test_the_search_field_says_what_back_and_forward_lead_to() -> None:
    out = _run("textFollowsBack")
    assert out["otherList"] == ["", [""]]
    assert (out["rows"], out["forward"]) == ("", "ab")


def test_a_sort_is_told_to_a_screen_reader_and_a_reload_of_its_own_is_not() -> None:
    assert _run("listSaid") == ["", "57 titles, Title (A–Z).", "57 titles, Title (A–Z)."]


def test_a_row_whose_keep_changed_meanwhile_is_left_alone_read_again_and_said_so() -> None:
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
