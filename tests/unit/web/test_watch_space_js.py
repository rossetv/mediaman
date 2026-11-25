"""watch/space.js and watch/keep.js run under node against a fake DOM: the storage meter and its
panel, with what it keeps, clears, scans and downloads again."""

from __future__ import annotations

import json
import re
import subprocess
from functools import cache
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "watch_space_harness.js"
JS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/js"
ALL = "all"


@cache
def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class TestMeter:
    def test_is_asked_for_once_the_page_has_loaded_and_shows_what_is_free_and_leaving(self):
        out = _run("meter")
        assert out["askedBeforeLoad"] == 0
        assert out["asked"] == ["/api/dashboard/stats"]
        assert out["text"] == "1.4 TB free3 leaving"
        assert out["phone"].startswith("1.4 TB free")
        assert out["label"] == ("Storage: 1.4 TB free of 8 TB. 3 titles scheduled for deletion")
        assert out["busy"] is False

    def test_the_leaving_soon_row_says_the_same_count_and_the_size(self):
        assert _run("meter")["link"] == "3 · 30 GB to reclaim"

    def test_says_each_state_in_its_name(self):
        out = _run("meterStates")
        said = {k: (look, label.split(". ")[0]) for k, (look, label) in out.items()}
        free = "Storage: 1.4 TB free of 8 TB"
        assert said == {
            "over": ("space-meter--warn", f"{free}, above your 80% limit"),
            "full": ("space-meter--bad", f"{free}, nearly full"),
            "oneOfTwo": ("space-meter--warn", f"{free}, above your 90% limit on /b"),
            "twoOfTwo": ("space-meter--warn", f"{free}, above your limits on /a and /b"),
            "fullAndOver": (
                "space-meter--bad",
                f"{free}, nearly full, above your 90% limit on /b",
            ),
            "unread": ("space-meter--off", "Storage: the disks can’t be read"),
            "missing": ("", f"{free}, a folder is missing"),
            "off": ("", f"{free}, deletions are off"),
            "failed": ("space-meter--off", "Storage, could not be loaded"),
        }


class TestPanel:
    def test_holds_the_page_out_of_reach_and_gives_it_and_the_focus_back(self):
        out = _run("openAndClose")
        assert out["open"] == {
            "hidden": False,
            "focused": "space-tab-storage",
            "held": ["nav", "main"],
            "menuFree": True,
            # a screen reader would hide the toasts, and their Undo, from an aria-modal panel
            "modal": False,
            "expanded": "true",
            "pushed": [["push", "#storage"]],
        }
        assert out["byPointer"] == "Storage"
        assert out["closed"]["held"] == ["space"]
        assert out["closed"]["inert"] is True
        assert out["closed"]["focused"].startswith("Storage: ")

    def test_goes_under_what_opens_over_it_and_leaves_the_focus_to_that(self):
        out = _run("otherOpensOver")
        assert out["under"] is True
        assert not out["focused"].startswith("Storage: ")

    def test_opens_at_the_tab_its_address_names_and_closes_to_the_meter(self):
        out = _run("address")
        assert out.pop("closed").startswith("Storage: ")
        assert out == {
            "values": ["storage", "scheduled", "deleted", None, None, None],
            "on": ["space-deleted"],
            "tabs": "Storage",
        }

    def test_tabs_move_by_arrow_key_replace_the_address_and_leave_one_pane_in_reach(self):
        out = _run("tabs")
        assert out["one"] == [
            "Scheduled for deletion, 3 titles",
            ["replace", "#storage-scheduled"],
        ]
        assert out["end"] == "true"
        assert out["reach"] == [True, True, False]

    def test_escape_closes_the_pinned_list_before_the_panel(self):
        out = _run("escOrder")
        assert out["pinned"] == {"kind": "list", "rows": ["Big", "Small"], "pressed": "true"}
        assert out["once"]["kind"] == ALL
        assert out["once"]["open"] is True
        assert out["once"]["focused"].startswith("Movies, ")
        assert out["twice"] == {"open": False}

    def test_escape_is_first_the_confirm_s_and_the_keep_menu_s(self):
        assert _run("escapeElsewhere") == {
            "underConfirm": True,
            "menu": {"open": True, "expanded": "false"},
            "then": False,
        }

    def test_a_pull_of_the_handle_far_or_fast_closes_it_and_any_other_springs_back(self):
        assert _run("drag") == {
            "far": ["translateY(130px)", True, "", False],
            "near": ["translateY(60px)", False, "", False],
            "fast": ["translateY(60px)", True, "", False],
            "twitch": ["translateY(20px)", False, "", False],
            "up": ["translateY(0px)", False, "", False],
            "cancelled": ["translateY(300px)", False, "", False],
        }

    def test_back_to_it_takes_the_focus_only_when_nothing_in_it_has_it(self):
        out = _run("backFocus")
        assert out == {"lost": "Storage", "kept": "Scheduled for deletion, 3 titles"}

    def test_a_failure_is_told_in_every_tab_and_try_again_fills_them(self):
        out = _run("panelFails")
        assert out["was"] == {
            "texts": [
                "Couldn’t load storage.",
                "Couldn’t load what is scheduled.",
                "Couldn’t load what was deleted.",
            ],
            "foot": True,
        }
        assert (out["rows"], out["foot"]) == (["1", "2", "3"], False)
        assert out["focused"] == "space-tab-storage"


class TestStorage:
    def test_the_list_follows_the_pointer_after_a_pause_and_stays_through_a_short_absence(self):
        assert _run("hover") == [ALL, ALL, "Big", "Show", "Show", "Show", ALL]

    def test_the_keyboard_opens_each_segment_it_comes_to_without_pinning_it(self):
        out = _run("segmentFocus")
        assert (out["rows"], out["next"]) == (["Big", "Small"], ["Show"])
        assert out["focused"].startswith("TV Shows, ")
        assert out["pressed"] == "false"

    def test_a_heavy_title_says_when_it_leaves_past_its_date_any_day(self):
        said = ["Leaving any day", "Leaving today", "1 day left", "2 days left"]
        assert _run("heavyLeaving") == said

    def test_a_pinned_list_stays_whatever_the_pointer_does_until_pressed_again(self):
        assert _run("pinned") == {"held": "Big", "left": "list", "again": ALL}

    def test_an_older_list_answering_last_never_replaces_a_newer_one(self):
        out = _run("lateList")
        assert out["waiting"] is True
        assert out["now"] == ["Newest"]
        assert out["after"] == ["Newest"]

    def test_a_list_that_fails_says_so_and_can_be_tried_again(self):
        out = _run("listFails")
        assert out["said"] == "Couldn’t load the titles."
        assert out["rows"] == ["Big", "Small"]
        assert out["focused"] == "Back to all space"

    def test_the_whole_list_is_a_press_away_while_the_heaviest_load_and_after_they_fail(self):
        every = ["See all 312 by size", "/watch?view=list&type=movie&sort=size_desc"]
        assert _run("seeAllMeanwhile") == {
            "loading": [False, every, False, None],
            "failed": [True, every, True, None],
        }

    def test_titles_that_add_up_to_more_than_is_used_share_what_is(self):
        out = _run("segments")
        assert out["names"] == ["movies", "tv", "free"]
        # of 1000: used, free, and what the filesystem keeps back
        assert (out["used"], out["free"], out["rest"]) == (813, 175, 12)
        assert out["tick"] == "90%"

    def test_several_disks_are_listed_and_only_a_shared_threshold_is_marked(self):
        assert _run("disks") == {
            "list": [
                "/media/movies1.4 TB free of 8 TB · above 90%",
                "/media/tv1.4 TB free of 8 TB",
                "/media/<b>Can’t be read",
            ],
            "tick": False,
            "line": "81% full overall, above your 90% limit on /media/movies, 1 disk can’t be read",
            "mixedTick": False,
        }

    def test_unreadable_disks_leave_the_library_sizes_and_can_be_tried_again(self):
        out = _run("unread")
        assert out["was"]["meter"] == ["Can’t read storage", " storage"]  # a phone drops the word
        assert "Can’t read the disks right now." in out["was"]["text"]
        assert out["was"]["segs"] == ["movies", "tv"]
        assert "Free" not in out["was"]["text"].replace("Free space is unknown", "")
        assert out["free"] == "1.4 TBfree of 8 TB"
        assert out["focused"] == "space-tab-storage"

    def test_the_missing_folders_and_deletions_off_stand_on_top_of_storage_and_scheduled(self):
        pane = {
            "roots": [
                "/m/<b>x</b> is empty or not mounted: 1 scheduled deletion is waiting",
                "/media/tv is empty or not mounted: 3 scheduled deletions are waiting",
            ],
            "off": "Deletions are off: set MEDIAMAN_DELETE_ROOTS to the folders mediaman may "
            "delete from.",
        }
        assert _run("notices") == [pane, pane]

    def test_what_a_scan_would_not_delete_is_not_promised(self):
        out = _run("wouldFree")
        assert out["on"] == {
            "after": "3 scheduled titles will free 30 GB",
            "said": "1.4 TB once the scheduled titles go",
            "notices": [],
        }
        for k in ("off", "dry"):
            assert out[k]["after"] == "3 scheduled titles would free 30 GB"
            assert out[k]["said"] == "1.4 TB if the scheduled titles went"
        assert out["dry"]["notices"] == [
            "Dry run is on: scans delete nothing until it is turned off."
        ]

    def test_a_dry_run_is_told_on_an_empty_list_too(self):
        assert _run("dryRunOnEmpty") == [
            "Dry run is on: scans delete nothing until it is turned off."
        ]

    def test_an_unknown_size_is_never_called_nothing(self):
        assert _run("noSize") == {
            "link": "1 · size not known",
            "linkName": "Manage scheduled deletions: 1 title, size not known",
            "after": "1 scheduled title, size not known",
            "said": "Scheduled titles have no size yet1 title, size not known. Keep any you want to stay."
            "See what is scheduled",
            "head": "1 title · size not known",
            "row": "Film 1Deletes in 3 daysMovie · Size not known · Added 11 months agoKeep",
            "deleted": "OldDeleted yesterdaySize not known",
        }

    def test_a_sum_that_lacks_a_size_is_said_to_be_the_least_it_can_be(self):
        out = _run("someSizesNotKnown")
        assert out["leaving"] == {
            "closed": "2 · at least 10 GB to reclaim",
            "link": "2 · at least 10 GB to reclaim",
            "after": "2 scheduled titles will free at least 10 GB",
            "said": "At least 1.4 TB once the scheduled titles go2 titles, at least 10 GB. "
            "Keep any you want to stay.See what is scheduled",
            "head": "2 titles · at least 10 GB to reclaim",
        }
        # Only the list's own sum counts what is no longer leaving.
        assert out["overdue"]["head"] == "1 title, 1 overdue · at least 10 GB to reclaim"
        assert out["overdue"]["closed"] == "1 · 10 GB to reclaim"
        assert out["overdue"]["link"] == "1 · 10 GB to reclaim"
        assert out["overdue"]["after"] == "1 scheduled title will free 10 GB"

    def test_types_scaled_to_fit_what_is_used_claim_no_share_of_the_disk(self):
        out = _run("scaled")
        assert out["fits"]["rows"] == ["47.6%", "23.2%", "0%", "3.4%", "17.5%"]
        assert out["scaled"]["rows"] == ["17.5%"]
        assert out["scaled"]["names"] == [
            "Movies, 3.8 TB, 312 films",
            "TV Shows, 1.9 TB, 64 shows",
            "Free, 1.4 TB, 18 per cent",
        ]

    def test_without_plex_and_nothing_to_show_it_says_connect_and_has_no_bar(self):
        out = _run("noPlex")
        assert out["bar"] is False
        assert out["foot"] is False
        assert out["link"] == "/settings"
        for pane in ("storage", "scheduled"):
            assert "Connect Plex to get started" in out[pane]
            assert "Library is healthy" not in out[pane]


class TestScheduled:
    def test_lists_every_row_and_counts_only_those_leaving(self):
        out = _run("scheduled")
        assert out["head"] == "2 titles, 1 overdue, 1 being deleted · 40 GB to reclaim"
        assert out["tab"] == "2"
        assert "2 leaving" in out["meter"]
        assert out["rows"] == [
            "Film 1Due, the next scan will tryMovie · 10 GB · Added 11 months agoKeep",
            "Film 2Being deleted nowMovie · 10 GB · Added 11 months agoKeep",
            "Show · Season 2Deletes tomorrowTV · 10 GB · Added 11 months agoKeep",
            '<b>"Hi"</b>Deletes in 3 daysMovie · 10 GB · Added 11 months agoKeep',
        ]
        assert out["links"] == ["movie-1", "movie-2", "movie-3", None]
        assert out["name"] == "Keep Show, Season 2"

    def test_a_row_past_its_date_says_the_words_the_server_gave_it(self):
        said = ["Leaving any day, the next scan will try", "Due, but a folder was missing"]
        assert _run("overdue") == {"plain": said, "dry": said, "off": said}

    def test_a_keep_takes_the_row_off_everywhere_moves_to_the_next_and_undo_puts_it_back(self):
        out = _run("keepAndUndo")
        assert out["fromPanel"] is True  # so Ctrl+Z undoes it over the panel
        assert out["kept"] == {
            "rows": ["1", "3"],
            "focused": "Keep Film 3",
            "posts": [["POST", "/api/watch/keep", {"media_item_id": "2", "days": 30}]],
            "toast": {"message": "Kept Film 2 for 30 days.", "undo": True},
            "meter": "1.4 TB free2 leaving",
            "link": "2 · 20 GB to reclaim",
            "card": ["1"],
        }
        assert out["undone"] == {
            "rows": ["1", "2", "3"],
            "focused": "Keep Film 2",
            "post": ["POST", "/api/watch/unkeep", {"media_item_id": "2"}],
            "meter": "1.4 TB free3 leaving",
        }

    def test_a_kept_row_fades_where_it_stands_as_the_rest_slide_and_the_next_comes_into_view(self):
        out = _run("keepMotion")
        assert out["gone"] == ["1", "0px", [0]]
        assert out["slid"] == [["translate(0px, 84px)"]] * 2
        assert out["seen"] == [["focus", True], ["reveal", "nearest", "smooth"]]
        assert out["focused"] == "Keep Film 2"

    def test_keeping_the_last_one_leaves_the_focus_on_the_empty_state(self):
        out = _run("keepLast")
        assert out["rows"] == []
        assert out["focused"] == "space-empty"
        assert "Nothing scheduled for deletion" in out["text"]

    def test_a_keep_the_server_says_cannot_be_undone_offers_no_undo(self):
        assert _run("keepNotToUndo") == {
            "rows": ["1", "3"],
            "toast": "Kept Film 2 for 30 days.",
            "undo": False,
        }

    def test_forever_goes_by_the_admins_route_and_offers_no_undo(self):
        assert _run("keepForever") == {
            "rows": ["2", "3"],
            "posts": [["FORM", "/api/media/1/keep", {"duration": "forever"}]],
            "toast": "Kept Film 1 forever.",
            "undo": False,
        }

    def test_a_title_whose_day_has_come_goes_by_the_admins_route_and_says_a_deletion_runs(self):
        assert _run("keepLate") == {
            "posts": [
                ["FORM", "/api/media/1/keep", {"duration": "30 days", "read_action": "none"}]
            ],
            "toast": "Already being deleted.",
            "undo": False,
        }

    def test_a_failed_keep_leaves_the_row_and_the_focus_on_its_button(self):
        assert _run("keepFails") == {
            "rows": ["1", "2", "3"],
            "focused": "Keep Film 2",
            "toast": "Not now",
            "busy": 0,
        }

    def test_a_failed_keep_moves_focus_only_out_of_the_closed_menu(self):
        assert _run("failedFocus") == {
            "fromMenu": "Keep Film 1",
            "moved": "space-tab-scheduled",
            "menu": False,
        }

    def test_the_next_keep_comes_into_view_at_once_and_stays_through_a_refresh(self):
        assert _run("keepRevealed") == {
            "seen": [["2", "auto"]],
            "rows": ["2", "3", "9"],
            "same": True,
            "focused": "Keep Film 2",
        }

    def test_so_it_does_while_the_rows_still_slide(self):
        assert _run("keepRevealedMoving") == {
            "seen": [["2", "smooth"]],
            "rows": ["2", "3", "9"],
            "same": True,
            "focused": "Keep Film 2",
        }

    def test_a_refresh_that_takes_the_focused_row_brings_the_next_keep_into_view(self):
        assert _run("focusRevealed") == {
            "seen": [["3", "nearest", "smooth"]],
            "focused": "Keep Film 3",
        }

    def test_an_overdue_title_kept_leaves_the_count_of_those_leaving(self):
        assert _run("keepLateCount") == {
            "rows": ["2"],
            "meter": "1.4 TB free1 leaving",
            "tab": "1",
        }

    def test_an_empty_list_says_the_library_is_healthy(self):
        out = _run("healthy")
        assert "Library is healthy" in out["text"]
        assert out["button"] is False

    def test_a_second_press_while_a_keep_runs_sends_nothing(self):
        assert _run("secondPress") == 1

    def test_a_keep_in_the_leaving_soon_row_takes_it_off_the_panel_too(self):
        out = _run("keepFromTheRow")
        assert "2 leaving" in out["meter"]
        assert out["rows"] == ["2", "3"]
        assert out["fromPanel"] is False

    def test_a_keep_counts_at_once_and_its_refresh_never_takes_away_a_row_undone_meanwhile(self):
        out = _run("undoRace")
        assert "2 leaving" in out["meter"]
        assert out["during"] == ["1", "2", "3"]
        assert out["after"] == ["1", "2", "3"]

    def test_a_card_kept_in_an_open_grid_is_taken_out_by_the_grid(self):
        out = _run("keepInAGrid")
        assert out["inGrid"] == {
            "took": [["1", "[data-watch-keep]"]],
            "focused": "Keep Film 2",
        }
        assert out["inRow"] == 0

    def test_a_card_undone_in_an_open_grid_stays_there_and_can_be_kept_again(self):
        assert _run("gridFades") == {
            "undone": {"in": True, "card": "watch-card", "inert": False},
            "again": False,
        }

    def test_a_scan_that_ends_during_a_failed_keep_still_refreshes(self):
        out = _run("scanDuringFailedKeep")
        assert out["during"] == ["1", "2", "3"]
        assert out["after"] == ["1", "2", "3", "9"]

    def test_an_older_refresh_answering_last_does_not_bring_a_kept_row_back(self):
        assert _run("olderRefresh") == {"kept": ["2", "3"], "after": ["2", "3"]}

    def test_clear_all_asks_first_and_leaves_the_focus_in_the_panel(self):
        out = _run("clear")
        assert out["declined"] == {"posts": 0, "rows": ["1", "2", "3"]}
        assert out["posts"] == [["POST", "/api/scan/clear-scheduled", None]]
        assert out["rows"] == []
        assert out["toast"] == "Cleared 3 scheduled deletions"
        assert out["focused"] == "space-empty"
        assert out["meter"] == "1.4 TB free"

    def test_a_second_press_of_clear_all_sends_nothing_and_the_busy_button_keeps_the_focus(self):
        assert _run("clearBusy") == {
            "busy": {"disabled": False, "aria": "true", "said": "true", "focused": True},
            "posts": 1,
            "inPanel": True,
        }

    def test_back_with_the_clear_confirm_open_takes_it_along_and_clears_nothing(self):
        out = _run("backOverConfirm")
        assert out.pop("focused").startswith("Storage: ")
        assert out == {"asked": True, "open": False, "posts": 0}

    def test_a_clear_that_fails_leaves_the_list_and_the_focus(self):
        out = _run("clearFails")
        assert out["rows"] == ["1", "2", "3"]
        assert "Slow down" in out["toast"]
        assert (out["enabled"], out["focused"]) == (True, True)


class TestRefresh:
    def test_changes_what_the_rows_the_head_and_the_notices_say_where_they_are(self):
        out = _run("refreshed")
        wait = "/media/tv is empty or not mounted: "
        assert out["was"] == {
            "rows": ["Deletes in 3 days"] * 2,
            "late": [False, False],
            "head": "2 titles · 20 GB to reclaim",
            "notices": [],
            "storage": 0,
        }
        assert out["now"] == {
            "rows": ["Due, but a folder was missing", "Deletes tomorrow"],
            "late": [True, False],
            "head": "1 title, 1 overdue · 20 GB to reclaim",
            "notices": [wait + "2 scheduled deletions are waiting"],
            "storage": 1,
        }
        assert out["later"]["rows"] == ["Deletes in 3 days"] * 2
        assert out["later"]["late"] == [False, False]
        assert out["later"]["notices"][0] == wait + "1 scheduled deletion is waiting"
        assert out["later"]["notices"][1].startswith("Deletions are off")
        assert out["later"]["storage"] == 2
        assert out["gone"] == []
        assert (out["same"], out["focused"]) == (True, True)

    def test_an_older_one_answering_last_changes_nothing(self):
        assert _run("refreshOrder") == {
            "now": ["9"],
            "after": ["9"],
            "meter": "1.4 TB free1 leaving",
        }

    def test_what_a_redownload_button_says_outlives_a_redraw(self):
        assert _run("redoneStays") == {
            "drawnAgain": True,
            "adding": ["Adding…", "true", True],
            "queued": "Queued",
            "again": ["Queued", "true", True],
        }


class TestThePageBehind:
    def test_a_clear_takes_the_leaving_soon_row_and_the_leaving_badges_and_tells_the_list(self):
        assert _run("pageAfterClear") == {
            "cards": [],
            "row": False,
            "badges": 0,
            "told": ["mm:watch-scheduled"],
            "asked": 0,
            "inPanel": True,
        }

    def test_a_scan_puts_new_cards_in_their_places_and_leaves_the_rest_be(self):
        assert _run("pageAfterScan") == {
            "cards": ["9", "1", "3", "2"],
            "same": True,
            "told": ["mm:watch-scheduled"],
            "asked": 2,
            "link": 1,
        }

    def test_a_scan_brings_the_row_that_was_gone_back_where_the_server_has_it(self):
        assert _run("rowAfterScan") == {
            "gone": True,
            "rows": ["leaving", "recent"],
            "cards": ["9"],
            "started": ["watch-row__viewport", "leaving"],
            "link": "1 · 10 GB to reclaim",
        }

    def test_what_a_scan_adds_is_given_to_the_open_grid_once_all_of_it_is_in_the_row(self):
        assert _run("gridAfterScan") == {
            "added": [["8", "9,1,8,2"], ["9", "9,1,8,2"]],
            "row": [["7", "7"]],
        }

    def test_an_answer_that_is_another_page_adds_nothing(self):
        assert _run("pageRedirected") == {
            "asked": 1,
            "cards": ["1", "2"],
            "rows": ["1", "2", "9"],
        }

    def test_a_row_s_see_all_is_fitted_once_its_new_cards_have_their_width(self):
        for came in ("rowGrown", "rowUndone"):
            assert _run(came) == {"before": 0, "after": ["leaving"]}, came

    def test_the_page_is_asked_for_only_for_new_cards_and_a_keep_outdates_the_answer(self):
        assert _run("pageAsked") == {"none": 0, "cards": ["1", "2"]}

    def test_a_card_that_goes_from_under_the_focus_hands_it_to_one_that_stays(self):
        assert _run("pageFocus") == {"cards": ["3"], "focused": "Keep Film 3"}

    def test_the_list_is_told_of_every_keep_that_is_in_and_of_none_that_failed(self):
        told = ["mm:watch-scheduled"]
        assert _run("listTold") == {"during": 0, "kept": told, "failed": 1, "mixed": 2}


class TestScan:
    def test_the_clock_counts_every_second_unread_and_stops_with_the_scan(self):
        out = _run("scanClock")
        said = "Scanning libraries"
        assert out["started"] == {
            "out": [
                said,
                f"{said}, 1s",
                f"{said}, 2s",
                f"{said}, 3s",
                f"{said}, 4s",
                "Finished in 4s",
            ],
            "read": said,
            "stopped": True,
        }
        already = "Scan already in progress"
        assert out["already"]["out"][2:5] == [already, already, f"{said}, 4s"]
        assert out["already"]["stopped"] is True

    def test_the_clock_starts_at_one_second_when_the_start_is_slow_to_answer(self):
        said = "Scanning libraries"
        assert _run("scanClockSlowStart") == [f"{said}, 1s", f"{said}, 2s"]

    def test_keeps_its_icon_and_focus_through_every_state_and_shows_what_it_found(self):
        out = _run("scan")
        redo = "#i-rotate-right"
        assert out["states"] == [
            ["Scan now", redo, ""],
            ["Scanning", redo, "Scanning libraries"],
            ["Scanning", redo, "Scanning libraries, Ns"],
            ["Scan complete", "#i-check", "Finished in Ns"],
            ["Scan now", redo, "A scan looks for titles to clear"],
        ]
        assert out["during"] == {
            "focused": "space-scan",
            "rows": ["1", "2", "3", "9"],
            "live": "Scan finished in Ns",
        }

    def test_a_second_press_starts_no_second_scan(self):
        assert _run("scanTwice") == {"posts": 1, "look": True, "aria": "true"}

    @pytest.mark.parametrize(
        ("end", "label", "said", "link"),
        [
            ("skipped", "Scan now", "Scan skipped: Plex is not configured", "/settings"),
            ("error", "Retry scan", "Scan failed: see the server log for details", None),
            ("notStarted", "Retry scan", "Couldn't start the scan. Too many scan triggers", None),
            ("lost", "Retry scan", "Lost track of the scan. Refresh to check its status.", None),
        ],
    )
    def test_an_end_that_found_nothing_is_told_beside_the_button_not_in_a_toast(
        self, end, label, said, link
    ):
        out = _run("scanEnds")[end]
        assert (out["label"], out["icon"], out["link"]) == (label, "#i-rotate-right", link)
        assert out["said"].startswith(said)
        assert out["live"] == said
        assert (out["toasts"], out["enabled"], out["focused"]) == (0, True, True)

    def test_a_scan_that_left_a_library_out_says_which_as_text_until_the_next_one(self):
        out = _run("scanEnds")["leftOut"]
        said = "inished in 3s. Left out: <b>TV</b> could not be read"
        assert re.sub(r"\d+s", "3s", out["said"]) == "F" + said
        assert re.sub(r"\d+s", "3s", out["live"]) == "Scan f" + said

    @pytest.mark.parametrize(
        ("end", "said", "look"),
        [
            ("done", "Scan finished in Ns", "success"),
            ("leftOut", "Scan finished in Ns. Left out: <b>TV</b> could not be read", ""),
            ("stale", "Scan finished in Ns. Refresh the page to see the results.", "success"),
            ("skipped", "Scan skipped: Plex is not configured", ""),
            ("error", "Scan failed: see the server log", "error"),
            ("notStarted", "Couldn't start the scan. Too many scan triggers", "error"),
            ("lost", "Lost track of the scan. Refresh to check its status.", "error"),
        ],
    )
    def test_an_end_with_the_panel_closed_is_told_in_a_toast_and_the_meter_was_busy_till_then(
        self, end, said, look
    ):
        out = _run("scanEndsClosed")[end]
        assert out["during"] == {"busy": ["true", "true"], "says": True}
        assert out["toasts"] == [[said, look]]
        assert (out["busy"], out["says"]) == ([None, None], False)


class TestDeleted:
    def test_a_row_is_downloaded_again_with_the_ids_stored_for_it(self):
        out = _run("deleted")
        payload = {"title": 'He said "Hi" & <b>', "tvdb_id": 371980, "season_number": 2}
        assert out["posts"] == [["POST", "/api/media/redownload", payload]]
        assert out["adding"] == ["Adding…", "true"]
        assert out["queued"] == [
            "Queued",
            "true",
            "#i-check",
            True,
            'He said "Hi" & <b> · Season 2 queued',
        ]

    def test_a_row_without_a_stored_id_has_no_button(self):
        out = _run("deleted")
        assert out["buttons"] == [1, 0]
        assert out["rows"][1] == "OldDeleted yesterdayFreed 8.7 GB"

    def test_a_poster_says_nothing_beside_the_title(self):
        assert _run("deleted")["posters"] == [["/api/poster/5", ""]]

    def test_the_total_and_the_history_stand_at_the_top(self):
        out = _run("deleted")
        assert (out["head"], out["history"]) == ("5 GB reclaimed in total", "/history")

    def test_a_failed_one_says_why_and_can_be_pressed_again(self):
        out = _run("redownloadFails")
        assert (out["text"], out["enabled"]) == ("Radarr is not set up", True)
        assert out["toast"]["message"] == "Couldn't re-download. Radarr is not set up"

    def test_nothing_deleted_yet_says_so(self):
        assert "No deletion history yet" in _run("noDeleted")


def test_an_expired_session_goes_to_sign_in_without_showing_a_failure_first():
    assert _run("expired") == {"to": "/login", "meter": "true"}


def test_a_session_that_ends_meanwhile_sends_a_scan_its_polls_and_a_clear_to_sign_in():
    gone = {"to": "/login", "toasts": 0, "rows": 3}
    assert _run("expiredActions") == {"scan": gone, "clear": gone, "poll": gone}


def test_the_panel_never_reloads_the_page():
    assert "reload" not in (JS / "watch/space.js").read_text()
