"""Where the focus goes after an Undo in the Watch list."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "list_changes_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_an_undo_gives_the_focus_back_to_the_keep_of_its_row() -> None:
    assert _run("undoFocus") == {"saved": True, "refused": True, "unread": True}


def test_an_undo_whose_row_is_not_there_puts_the_focus_on_a_row_in_sight_or_the_filter() -> None:
    assert _run("undoElsewhere") == {"page": True, "filter": "anime"}


def test_an_undo_takes_no_focus_from_where_it_is_or_from_what_is_open_over_the_list() -> None:
    assert _run("undoLeavesFocus") == {"details": True, "dialog": True, "typing": "watch-search"}
