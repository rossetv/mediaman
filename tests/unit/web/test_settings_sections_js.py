"""The settings page's libraries, overview and newsletter say what is true and follow a change.

The real scripts run under node against a small fake DOM, so these check what
the page ends up showing, not how the scripts are written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.scanner.runner import _filter_libraries_by_disk, read_disk_threshold
from tests.helpers.factories import insert_settings

HARNESS = Path(__file__).parent / "js" / "settings_sections_harness.js"


def _run(scenario: str, *args: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario, *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _scanner_does(entry: object) -> str:
    try:
        limit = read_disk_threshold(entry)
    except ValueError:
        return "never"
    return "limited" if limit else "always"


def _card_says(text: str) -> str:
    if "always scans" in text:
        return "always"
    if "t be read" in text:
        return "never"
    return "limited"


def test_a_library_is_a_button_that_says_whether_it_is_chosen() -> None:
    def look(pressed: str) -> list[str]:
        return ["BUTTON", "button", pressed]

    on, off = look("true"), look("false")
    out = _run("pills")
    assert out["before"] == [on, off]
    assert out["after"] == [off, on]
    assert out["restored"] == [on, off]
    assert out["shown"] == [off, on]


def test_a_card_says_what_the_scanner_does_with_its_stored_limit() -> None:
    entries: list[object] = [
        None,
        {},
        [],
        "",
        {"path": "", "threshold": 80},
        {"threshold": 80},
        {"path": "/a", "threshold": 0},
        {"path": "/a", "threshold": -5},
        {"path": "/a", "threshold": 80},
        {"path": "/a", "threshold": "80"},
        {"path": "/a", "threshold": 80.5},
        {"path": "/a", "threshold": "80.5"},
        {"path": "/a", "threshold": "many"},
        {"path": "/a", "threshold": [80]},
        {"path": ["/a"], "threshold": 80},
        {"path": "/a\0b", "threshold": 80},
        {"path": "", "threshold": "many"},
        "odd",
        7,
        ["/a", 80],
    ]
    says = [_card_says(text) for text in _run("stored", json.dumps(entries))]
    assert says == [_scanner_does(entry) for entry in entries]


def test_a_card_says_what_the_scanner_does_with_the_whole_stored_setting(conn) -> None:
    settings: list[object] = ["odd", ["x"], 5, [], {}]
    scanned = []
    for stored in settings:
        conn.execute("DELETE FROM settings WHERE key='disk_thresholds'")
        insert_settings(conn, disk_thresholds=json.dumps(stored))
        scan, left_out = _filter_libraries_by_disk(conn, ["1"], {})
        scanned.append({("1",): "always", (): "never"}[tuple(scan)])
        assert left_out == ([] if scan else ["1"])
    assert [_card_says(text) for text in _run("wholes", json.dumps(settings))] == scanned


def test_a_limit_with_no_folder_is_said_to_be_ignored() -> None:
    out = _run("typing")
    assert "will scan" in out["limited"]
    assert "no path" in out["noPath"].lower() and "always scans" in out["noPath"]
    assert "scheduled scans skip" in out["below"]
    assert "always scans" in out["noLimit"]


def test_cards_of_unreadable_limits_change_once_a_save_has_mended_them() -> None:
    out = _run("mended")
    assert [_card_says(t) for t in out["before"]] == ["never", "never"]
    assert [_card_says(t) for t in out["typed"]] == ["limited", "never"]
    assert [_card_says(t) for t in out["saved"]] == ["limited", "always"]


def test_a_late_read_of_another_folder_is_not_shown() -> None:
    assert "Below threshold" in _run("lateRead")


def test_only_a_missing_folder_is_said_to_leave_its_library_always_scanned() -> None:
    out = _run("unreadDisk")
    assert "Folder not found" in out["missing"] and "always scanned" in out["missing"]
    for answer in ("hidden", "slow"):
        assert "always" not in out[answer] and "If a scheduled scan" in out[answer]
    # nor does the page know that a scheduled scan can read what it may not
    assert "scans do" not in out["hidden"]
    assert "scan" not in out["lost"]


def test_every_card_that_says_a_library_is_left_out_speaks_of_scheduled_scans() -> None:
    entries = [{"path": "/a", "threshold": limit} for limit in (95, 100, "many")]
    cards = _run("stored", json.dumps(entries)) + _run("wholes", '["odd"]')
    for text in cards:
        assert "scheduled scans skip" in text


def test_the_sections_say_what_scan_now_does_with_the_limits() -> None:
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)
    libraries = env.get_template("settings/_sec_libraries.html").render(settings={})
    assert (
        "Scheduled scans" in libraries
        and "Scan now takes a library however full its disk is" in libraries
    )
    assert "leaves out one whose saved limit cannot be read" in libraries


def test_typing_the_shown_values_again_mends_an_unreadable_limit() -> None:
    out = _run("retyped", json.dumps({"path": "/a", "threshold": "80.5"}), "/a", "80")
    never = {"says": out["untouched"]["says"], "changes": None, "sameThing": True}
    assert _card_says(never["says"]) == "never"
    assert out["untouched"] == never
    assert _card_says(out["typedSays"]) == "limited"
    assert _card_says(out["typed"]["says"]) == "limited"
    assert out["typed"]["changes"] == {"1": {"path": "/a", "threshold": 80}}
    assert out["typed"]["sameThing"] is False
    # put back by the page itself, it is no change any more
    assert out["shownAgain"]["changes"] is None
    assert out["discarded"] == never


def test_typing_no_limit_over_an_unreadable_one_does_not_set_the_library_free() -> None:
    out = _run("retyped", json.dumps({"path": "/a", "threshold": "many"}), "/a", "0")
    assert out["typed"] == out["untouched"]
    assert _card_says(out["typed"]["says"]) == "never"


_UNREADABLE = [
    {"path": "/a", "threshold": "9x"},
    {"path": "/a\0", "threshold": 9},
    {"path": ["/a"], "threshold": 9},
]


def _insert(value: str, data: str) -> list[object]:
    return ["threshold", value, "insertText", data]


def _delete(value: str) -> list[object]:
    return ["threshold", value, "deleteContentBackward", None]


def _edits(entry: object, *runs: list[object]):
    return _run("edits", json.dumps(entry), json.dumps(runs))


def test_an_undone_keystroke_over_an_unreadable_limit_leaves_no_change() -> None:
    path = ["path", "/ab", "insertText", "b"], ["path", "/a", "deleteContentBackward", None]
    undo = ["threshold", "9", "historyUndo", None]
    outcomes = _edits(
        {"path": "/a", "threshold": "9x"},
        list(path),
        [_insert("95", "5"), _delete("9")],
        [_delete(""), undo],
        [_insert("5", "5"), undo],
        # a number field gives what is no number as empty
        [[*_insert("", "e"), True], _delete("9")],
        # the arrows of the field
        [["threshold", "10", None, None], ["threshold", "9", None, None]],
        # what was kept meanwhile is taken back too
        [_insert("95", "5"), "redraw", _delete("9")],
        [path[0], "redraw", path[1]],
    )
    for out in outcomes:
        assert (out["changes"], out["sameThing"]) == (None, True)
        assert _card_says(out["says"]) == "never"


def test_only_a_limit_typed_anew_replaces_an_unreadable_one() -> None:
    folder = ["path", "/new", "insertText", "w"]
    for entry in _UNREADABLE:
        for out in _edits(
            entry,
            [folder],
            [folder, "redraw"],
            [folder, "elsewhere"],
            [folder, "checked"],
            [folder, _insert("95", "5"), _delete("9")],
            [folder, _delete("")],
            [folder, ["threshold", "0", "insertText", "0"]],
            [["path", "", "deleteContentBackward", None], _delete("")],
        ):
            assert out["changes"] is None
            assert _card_says(out["says"]) == "never" and "a limit, then save" in out["says"]
        kept = _edits(entry, [folder, "redraw"], [folder, "elsewhere"], [folder, "checked"])
        assert [out["shows"] for out in kept] == [["/new", "9"]] * 3


def test_the_shown_number_typed_in_whole_counts_as_a_limit() -> None:
    folder = ["path", "/new", "insertText", "w"]
    for entry in _UNREADABLE:
        for out in _edits(
            entry,
            [folder, _insert("9", "9")],
            [folder, _delete(""), _insert("9", "9")],
            [folder, _delete(""), _insert("9", "9"), "redraw"],
            [folder, _delete(""), _insert("9", "9"), "checked"],
        ):
            assert out["changes"] == {"1": {"path": "/new", "threshold": 9}}
            assert _card_says(out["says"]) == "limited"
    (other,) = _edits(_UNREADABLE[0], [_insert("95", "5")])
    assert other["changes"] == {"1": {"path": "/a", "threshold": 95}}


def test_a_folder_typed_over_a_limit_another_page_has_mended_changes_nothing() -> None:
    folder = ["path", "/new", "insertText", "w"]
    (only, limit, undone) = _edits(
        _UNREADABLE[0],
        [folder, "mendedElsewhere"],
        [_insert("95", "5"), "mendedElsewhere"],
        [_insert("95", "5"), _delete("9"), "mendedElsewhere"],
    )
    assert only["changes"] is None and only["shows"] == ["/else", "70"]
    assert limit["changes"] == {"1": {"path": "/a", "threshold": 95}}
    assert undone["changes"] is None


def test_the_libraries_say_to_set_up_plex_only_when_it_is_not_set_up() -> None:
    out = _run("plexAnswers")
    hints = {name: pills for name, (pills, _) in out.items()}
    assert "Save Plex URL" in hints["notSetUp"]
    assert "Could not load" in hints["failed"]
    assert "No libraries found" in hints["none"]
    assert "disk thresholds are unchanged" in out["failed"][1]


def test_the_overview_shows_the_saved_schedule_and_storage() -> None:
    out = _run("overview")
    # the schedule the server has, not what the browser put back over it
    assert out["loaded"]["scan"] == "Mon, Thu 09:00 Timezone: Asia/Kolkata"
    assert out["loaded"]["storage"] == ["Not configured", 0]
    assert out["saved"]["scan"] == "Tuesday 10:00 Timezone: Europe/London"
    assert out["saved"]["storage"] == ["1 TB of 4 TB used · 3 TB free", 1]
    assert out["other"]["storage"][0].startswith("2 TB")
    # what the save sent, not what was chosen since
    assert out["during"]["scan"] == "Saturday 10:00 Timezone: Europe/London"
    assert out["during"]["storage"] == ["Not configured", 0]


def test_open_recipients_follow_the_subscribers_and_keep_what_was_chosen() -> None:
    out = _run("recipients")

    def person(n: int, ticked: bool = True) -> list[object]:
        return [f"p{n}@example.com", ticked]

    assert out["opened"] == [person(1), person(2)]
    assert out["added"] == [person(1), person(2), person(3)]
    # once someone is left out, nobody new is put in unasked
    assert out["addedAfterAChoice"] == [person(1), person(2, False), person(3), person(4, False)]
    assert out["focusKept"] is True
    assert out["removed"] == [person(2, False), person(3), person(4, False)]
    assert out["optedOut"] == 3
    assert "No active" in out["none"]
    assert out["again"] == [[person(6)], False]


def test_recipients_that_cannot_be_read_again_stay_as_they_were_and_say_so() -> None:
    out = _run("staleRecipients")
    kept = [["p1@example.com", True], ["p2@example.com", False]]
    assert out["failed"]["ticks"] == kept
    assert "out of date" in out["failed"]["note"]
    assert out["failed"]["subscribers"] == ["p1@example.com", "p2@example.com"]
    assert len(out["failed"]["errors"]) == 1
    # the next answer that comes brings the list up to date, and leaves out who was left out
    assert out["recovered"]["ticks"] == [
        *kept,
        ["p3@example.com", False],
        ["p4@example.com", False],
    ]
    assert out["recovered"]["note"] == ""
    assert len(out["recovered"]["subscribers"]) == 4
    assert out["failedAgain"]["ticks"] == 4 and out["failedAgain"]["note"]
    assert out["reopened"] == {"ticks": 3, "note": ""}


def test_the_send_panel_asks_for_the_base_url_while_it_is_empty_and_escape_shuts_it() -> None:
    out = _run("panelWords")
    assert out["filled"] == ""
    assert out["emptyOnOpen"] == "Set Base URL under General & Schedule first"
    assert "is-err" in out["noteClass"].split()
    assert out["typed"] == ""
    assert out["cleared"] == "Set Base URL under General & Schedule first"
    assert out["escape"] == {"open": False, "expanded": "false", "focused": True}


def test_escape_on_the_button_that_opened_the_send_panel_shuts_it() -> None:
    out = _run("escapeFromOpener")
    opened = {"open": True, "label": "Close", "expanded": "true", "focused": True}
    shut = {"open": False, "label": "Select recipients", "expanded": "false", "focused": True}
    assert out["before"] == opened
    # nothing happens while the send is under way, or for another key
    assert out["sending"] == opened
    assert out["otherKey"] == opened
    assert out["after"] == shut
    assert out["again"] == shut


def test_the_send_panel_names_what_is_missing_as_the_server_does() -> None:
    out = _run("setupNotes")
    mail = "set up Email (Mailgun) under Integrations"
    base = "set Base URL under General & Schedule"
    assert out["noMail"] == "Set up Email (Mailgun) under Integrations first"
    assert out["nothing"] == f"Set up Email (Mailgun) under Integrations and {base} first"
    assert (
        out["mailAndFtp"]
        == f"{mail[0].upper()}{mail[1:]} and make Base URL start with http:// or https:// first"
    )
    assert out["ftp"] == "Make Base URL start with http:// or https:// first"
    assert out["unsaved"].startswith("Save Settings first")
    assert out["saved"] == ""


def test_the_note_is_read_again_after_a_save_and_when_focus_or_a_pointer_returns() -> None:
    out = _run("noteRefresh")
    assert out["typed"].startswith("Save Settings first")
    assert out["stale"] == out["typed"]
    assert (out["afterSave"], out["afterFocus"], out["afterPointer"]) == ("", "", "")


def test_a_refused_send_says_the_missing_setting_once() -> None:
    out = _run("refusedOnce")
    assert out["before"] == "Set Base URL under General & Schedule first"
    assert out["note"] == ""
    assert out["status"] == "Set Base URL under General & Schedule first"
    assert out["noteAfterFocus"] == ""
    # a changed setting makes the answer stale and the note speaks for the new state
    assert out["changed"] == {
        "note": "Make Base URL start with http:// or https:// first",
        "status": "",
    }


def test_removing_someone_who_unsubscribed_says_that_it_is_forgotten() -> None:
    active, opted_out = _run("removalWords")
    assert "add them again" in active and "unsubscribed" not in active
    assert "have unsubscribed" in opted_out and "forgets that" in opted_out


def test_the_focus_of_a_recipient_taken_off_the_list_goes_to_the_one_beside_it() -> None:
    out = _run("recipientFocus")
    assert out["to"] == [
        "recipient-3",
        "recipient-1",
        "btn-send-newsletter",
        "btn-send-newsletter",
    ]
    # and the page stays where it is
    assert out["still"] == [True, True, True]


def test_the_page_says_the_libraries_are_loading_until_plex_answers() -> None:
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)
    html = env.get_template("settings/_sec_libraries.html").render(settings={})
    assert "Loading" in html
    assert "Save Plex URL" not in html
