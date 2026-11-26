"""Back and a press-and-hold preview, run under node against a fake DOM and a fake history. One
node run gives every scenario's outcome."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "watch_preview_back_harness.js"


@pytest.fixture(scope="module")
def out() -> dict:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_preview_opened_by_a_finger_takes_one_history_entry(out) -> None:
    assert (out["opened"]["open"], out["opened"]["entries"], out["opened"]["index"]) == (True, 2, 1)


def test_back_closes_the_preview_and_stays_on_the_page(out) -> None:
    assert out["backClosesIt"] == {"entries": 2, "index": 0, "open": False, "url": "/watch"}


@pytest.mark.parametrize("how", ["tapOutside", "escape", "scrolled"])
def test_closing_the_preview_another_way_leaves_no_entry_behind(out, how) -> None:
    assert (out[how]["open"], out[how]["index"]) == (False, 0)


def test_a_scroll_that_closes_it_is_not_undone_by_the_history(out) -> None:
    assert out["scrolled"]["y"] == 300


def test_opening_the_title_from_it_replaces_its_entry_and_back_leaves_the_page_as_it_was(
    out,
) -> None:
    assert out["openedTheTitle"] == {
        "opened": {"entries": 2, "index": 1},
        "back": {"entries": 2, "index": 0, "url": "/watch"},
    }


def test_a_mouse_preview_never_touches_the_history(out) -> None:
    assert out["mouse"] == {"open": True, "entries": 1, "went": 0}
