"""What becomes of a dialog opened from a title as the title closes, changes or is drawn anew."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "title_dialogs_harness.js"


def _run(scenario: str):
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_back_closes_a_dialog_with_the_title_it_was_opened_from() -> None:
    assert _run("back") == {
        "closed": 1,
        "inert": False,
        "at": "show-300",
        "forward": "admin-delete",
    }


def test_back_to_the_title_before_closes_a_dialog_of_the_one_after() -> None:
    assert _run("backToTitle") == {"closed": 1, "title": "Brightwater", "in": True}


def test_a_dialog_gives_the_focus_back_to_its_button_as_drawn_since_or_to_the_title() -> None:
    assert _run("redrawn") == [["admin-delete", True], ["ttl-title", True]]


def test_a_title_deleted_from_its_page_leaves_the_rows_and_its_other_cards_say_not_in_library() -> (
    None
):
    def want(tmdb: str) -> dict:
        return {
            "taken": ["own", "continue"],
            "at": "other",
            "badges": 0,
            "links": [[tmdb, f"/watch#title={tmdb}"]] * 2,
        }

    assert _run("deleted") == {"movie-120": want("tmdb-movie-77"), "show-300": want("tmdb-tv-77")}
