"""The AI's answer on the Watch page: the reason under each card, the group that leads, and the
titles shown while the run is still checking them. Run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_ask_cards_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_because_you_liked_lead_is_bold_and_the_rest_of_the_reason_follows() -> None:
    out = _run("reasons")
    assert out["whys"][0] == {
        "text": "Because you liked Nine Locks · the same dry humour.",
        "bold": ["B:Because you liked Nine Locks"],
        "kids": 1,
    }
    assert out["whys"][2]["bold"] == ["B:Because you watched Orchard"]


def test_a_reason_is_text_never_markup_and_an_empty_one_draws_no_line() -> None:
    out = _run("reasons")
    assert out["imgs"] == 0
    assert out["whys"][1] == {"text": "<img src=x onerror=alert(1)>", "bold": [], "kids": 0}
    assert len(out["whys"]) == 3


def test_the_library_leads_when_the_best_fit_is_there() -> None:
    assert _run("leadOwned")["heads"] == ["In your library", "Not in your library"]


def test_the_group_to_download_leads_when_the_server_says_so_and_holds_through_a_narrowing() -> (
    None
):
    out = _run("leadHolds")
    assert out["before"] == ["Not in your library", "In your library"]
    assert "Funnier" in out["sent"]
    # A narrowing run's titles so far do not replace the answer it narrows.
    assert out["during"] == [["Film 2", "Film 1"], ["Not in your library", "In your library"]]
    assert out["after"] == ["Not in your library", "In your library"]


def test_a_running_poll_draws_the_titles_checked_so_far_with_outlines_after_them() -> None:
    out = _run("partial")
    assert out["none"][0] == []
    assert out["none"][1] > 0
    cards, outlines, heads, whys = out["one"]
    assert (cards, heads, whys) == (
        ["Film 2"],
        ["Not in your library", "AI picks"],
        ["It is funny."],
    )
    assert outlines > 0
    assert out["three"][0] == ["Film 2", "Film 3", "Film 1"]
    assert out["three"][1] > 0
    # Done: the outlines go and the groups stand in the order the server gave.
    assert out["done"] == [
        ["Film 2", "Film 3", "Film 1"],
        0,
        ["Not in your library", "In your library"],
    ]
