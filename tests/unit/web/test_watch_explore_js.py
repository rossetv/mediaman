"""A person's page on Watch, run under node against a fake DOM with the search page as a stub:
what heads the titles the library does not hold."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_explore_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_titles_not_held_are_offered_for_download_only_where_both_kinds_can_be() -> None:
    assert _run("personHeads") == {
        "both": ["In your library", "Available to download"],
        "films": ["In your library", "Not in your library"],
        "none": ["In your library", "Not in your library"],
    }


def test_a_person_opened_under_a_flying_photo_is_told_once_it_lands() -> None:
    assert _run("heldUntilLanded") == {
        "flying": {"facts": "", "cards": None, "atOnce": True},
        "landed": {"facts": "Actor · 1 in your library, 0 more", "cards": 2},
    }


def test_a_persons_titles_come_twelve_at_a_time_a_frame_apart() -> None:
    # 30 titles and 2 headings: the first page at once, each part after it a frame later,
    # all of it said once the last part is in.
    assert _run("batches") == [[12, True, True], [12, False, True], [8, False, False]]


def test_what_stands_below_a_person_waits_for_the_photo_to_land() -> None:
    assert _run("keptBack") == {
        "flying": {"known": True, "grid": True},
        "landed": {"known": False, "grid": False},
    }


def test_rows_under_a_title_come_back_once_the_persons_photo_lands() -> None:
    assert _run("closedUnderATitle") == {"title": [False, True], "rows": None, "pushed": None}
