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
    if "t scanned" in text or "nothing is scanned" in text:
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
    assert "will skip" in out["below"]
    assert "always scans" in out["noLimit"]


def test_cards_of_unreadable_limits_change_once_a_save_has_mended_them() -> None:
    out = _run("mended")
    assert [_card_says(t) for t in out["before"]] == ["never", "never"]
    assert [_card_says(t) for t in out["typed"]] == ["limited", "never"]
    assert [_card_says(t) for t in out["saved"]] == ["limited", "always"]


def test_a_late_read_of_another_folder_is_not_shown() -> None:
    assert "will skip" in _run("lateRead")


def test_a_disk_that_cannot_be_read_is_said_to_be_scanned_but_a_lost_request_is_not() -> None:
    unread, lost = _run("unreadDisk")
    assert "Folder not found" in unread and "always scanned" in unread
    assert "scanned" not in lost


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


def test_the_page_says_the_libraries_are_loading_until_plex_answers() -> None:
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)
    html = env.get_template("settings/_sec_libraries.html").render(settings={})
    assert "Loading" in html
    assert "Save Plex URL" not in html
