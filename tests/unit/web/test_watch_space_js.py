"""watch/space.js and watch/keep.js run under node against a fake DOM: the storage meter and its
panel, with what it keeps, clears, scans and downloads again."""

from __future__ import annotations

import json
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
            "modal": True,
            "expanded": "true",
            "pushed": [["push", "#storage"]],
        }
        assert out["byPointer"] == "Storage"
        assert out["closed"]["held"] == ["space"]
        assert out["closed"]["inert"] is True
        # closed, it must not count as the dialog that owns the keyboard
        assert (out["before"], out["closed"]["modal"]) == (False, False)
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
                "/media/<b>Can’t be read",
            ],
            "tick": False,
            "line": "6.5 TB used, 81%, 1 disk can’t be read",
            "mixedTick": False,
        }

    def test_unreadable_disks_leave_the_library_sizes_and_can_be_tried_again(self):
        out = _run("unread")
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
            "Film 1Due, goes with the next scanMovie · 10 GB · Added 11 months agoKeep",
            "Film 2Being deleted nowMovie · 10 GB · Added 11 months agoKeep",
            "Show · Season 2Deletes tomorrowTV · 10 GB · Added 11 months agoKeep",
            '<b>"Hi"</b>Deletes in 3 daysMovie · 10 GB · Added 11 months agoKeep',
        ]
        assert out["links"] == ["movie-1", "movie-2", "movie-3", None]
        assert out["name"] == "Keep Show, Season 2"

    def test_an_overdue_row_says_what_holds_it_and_no_other_row_s_reason(self):
        assert _run("overdue") == {
            "missing": ["Due, but a folder is missing", "Due, goes with the next scan"],
            "dry": ["Due, but dry run is on"] * 2,
            "off": ["Due, but deletions are off"] * 2,
        }

    def test_a_keep_takes_the_row_off_everywhere_moves_to_the_next_and_undo_puts_it_back(self):
        out = _run("keepAndUndo")
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

    def test_forever_goes_by_the_admins_route_and_offers_no_undo(self):
        assert _run("keepForever") == {
            "rows": ["2", "3"],
            "posts": [["FORM", "/api/media/1/keep", {"duration": "forever"}]],
            "toast": "Kept Film 1 forever.",
            "undo": False,
        }

    def test_a_title_whose_day_has_come_goes_by_the_admins_route_and_says_a_deletion_runs(self):
        assert _run("keepLate") == {
            "posts": [["FORM", "/api/media/1/keep", {"duration": "30 days"}]],
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

    def test_a_second_press_while_a_keep_runs_sends_nothing(self):
        assert _run("secondPress") == 1

    def test_a_keep_in_the_leaving_soon_row_takes_it_off_the_panel_too(self):
        out = _run("keepFromTheRow")
        assert "2 leaving" in out["meter"]
        assert out["rows"] == ["2", "3"]

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

    def test_back_with_the_clear_confirm_open_takes_it_along_and_clears_nothing(self):
        out = _run("backOverConfirm")
        assert out.pop("focused").startswith("Storage: ")
        assert out == {"asked": True, "open": False, "posts": 0}

    def test_a_clear_that_fails_leaves_the_list_and_the_focus(self):
        out = _run("clearFails")
        assert out["rows"] == ["1", "2", "3"]
        assert "Slow down" in out["toast"]
        assert (out["enabled"], out["focused"]) == (True, True)


class TestScan:
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


def test_the_panel_never_reloads_the_page():
    assert "reload" not in (JS / "watch/space.js").read_text()
