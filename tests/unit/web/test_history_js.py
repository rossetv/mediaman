"""history.js run under node against a fake DOM: rows fold with an animation, copy, link."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "history_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_an_open_row_plays_its_exit_before_it_folds_and_drops_its_link() -> None:
    out = _run("close")
    assert out["afterClick"] == {"open": True, "closing": True}
    assert "event=5" in out["url"]
    assert out["settled"] == {"open": False, "closing": False}
    assert out["urlAfter"] == "http://mm.test/history?page=2"


def test_reduced_motion_folds_the_row_at_once() -> None:
    out = _run("reduced")
    assert out["afterClick"] == {"open": False, "closing": False}
    assert out["urlAfter"] == "http://mm.test/history?page=2"


def test_opening_a_row_is_left_to_the_browser_and_puts_its_id_in_the_address() -> None:
    out = _run("open")
    assert out["prevented"] is False
    assert out["url"] == "http://mm.test/history?page=2&event=6"


def test_copy_puts_the_labels_and_values_on_the_clipboard_and_keeps_the_row_open() -> None:
    out = _run("copy")
    assert out["copied"] == [
        "Time: 25 Nov 2026, 01:45:03 (2026-11-24 20:15:03 UTC)\nWho: admin\nIP address: 1.2.3.4"
    ]
    assert out["toasts"] == ["Details copied"]
    assert out["stillOpen"] is True


def test_what_is_below_slides_up_with_a_closing_row_and_is_let_go_once_it_has_folded() -> None:
    out = _run("close")
    slide = {"from": "none", "to": "translateY(-300px)", "ms": 200}
    assert out["moving"] == [{"name": n, **slide, "cancelled": False} for n in ("second", "after")]
    assert all(m["cancelled"] for m in out["moves"])


def test_what_is_below_an_opening_row_slides_down_from_where_it_was() -> None:
    out = _run("open")
    assert [(m["name"], m["from"], m["to"]) for m in out["moves"]] == [
        ("after", "translateY(-300px)", "none")
    ]


def test_reduced_motion_slides_nothing() -> None:
    assert _run("reduced")["moves"] == _run("open-reduced")["moves"] == []


def test_times_are_in_the_viewers_time_zone_and_the_details_keep_utc() -> None:
    line, full = _run("times")["times"][0]
    assert line == "25 Nov 2026, 01:45"
    assert full == "25 Nov 2026, 01:45:03 (2026-11-24 20:15:03 UTC)"


def test_a_time_that_cannot_be_read_stays_as_the_page_sent_it() -> None:
    assert _run("times")["times"][1] == ["stored UTC", "2026-11-24 20:15:03 UTC"]
