"""The Watch list view and the admin actions, run under node against a fake DOM.

Nothing here reloads the page: every action ends with the list changed in place.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_list_harness.js"


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
        ["postForm", "/api/media/m2/keep", {"duration": "forever"}],
        ["post", "/api/media/m2/unprotect", None],
    ]
    assert out["toast"] == "Kept Saltmarsh forever."


def test_changing_an_existing_keep_offers_no_undo() -> None:
    out = _run("keepAgain")
    assert out["call"] == ["postForm", "/api/media/m3/keep", {"duration": "30 days"}]
    assert out["undo"] is False


def test_a_second_press_while_a_keep_runs_sends_nothing() -> None:
    assert _run("keepTwice") == {"calls": 1}


def test_a_failed_keep_says_why_and_reads_nothing_again() -> None:
    out = _run("keepFails")
    assert out["errors"] == ["Couldn't save keep. Too many keep operations"]
    assert (out["fetches"], out["toasts"]) == ([], 0)


def test_removing_a_keep_turns_the_row_back_in_place() -> None:
    out = _run("removeKeep")
    assert out["call"] == ["post", "/api/media/m3/unprotect", None]
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
        {"duration": "30 days", "season_ids": ["s1"]},
    ]
    assert (out["closed"], out["kept"]) == (True, True)
    assert out["history"] == []


def test_the_detail_can_keep_and_delete_with_a_plain_wrapper_and_no_list_on_the_page() -> None:
    out = _run("detailCalls")
    assert out["keys"] == ["confirmDelete", "keep", "pickSeasonDelete", "removeKeep"]
    assert out["calls"] == [
        ["postForm", "/api/media/42/keep", {"duration": "7 days"}],
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
    assert out["cleared"] == {"q": "", "clearHidden": True}
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
    assert out["call"] == ["postForm", "/api/media/m1/keep", {"duration": "forever"}]
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
    assert out["busy"] is False
    assert out["label"] == "Keep"
    assert (out["triggerDisabled"], out["deleteDisabled"]) == (False, False)


def test_removing_a_keep_takes_away_the_undo_of_the_keep_it_removes() -> None:
    out = _run("removeKeepRetiresUndo")
    assert (out["before"], out["after"]) == (False, True)
    assert out["messages"] == ["Kept Copperline for 30 days.", "Removed the keep on Copperline."]
    # The retired Undo ran nothing: no unkeep request followed.
    assert out["calls"] == ["/api/watch/keep", "/api/media/m1/unprotect"]
