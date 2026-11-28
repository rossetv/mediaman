"""The AI's answer on the Watch page: the reason under each card, the group that leads, the
titles shown while the run is still checking them, and how a question with no answer ends. Run
under node against a fake DOM."""

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


def test_the_lead_the_server_names_is_bold_whole_even_where_the_title_has_a_colon() -> None:
    whys = _run("leads")["whys"]
    assert whys[0] == {
        "text": "Because you liked Star Wars: Episode IV · a space saga.",
        "bold": ["B:Because you liked Star Wars: Episode IV"],
        "kids": 1,
    }


def test_a_reason_with_no_lead_or_one_it_does_not_start_with_is_plain_and_whole() -> None:
    whys = _run("leads")["whys"]
    for why, text in zip(
        whys[1:],
        [
            "Because you liked Nine Locks: the same dry humour.",
            "Because you liked Nine Locks: the same dry humour.",
            "A heist done right.",
        ],
        strict=True,
    ):
        assert why == {"text": text, "bold": [], "kids": 0}


def test_a_reason_is_text_never_markup_and_an_empty_one_draws_no_line() -> None:
    out = _run("reasons")
    assert out["imgs"] == 0
    assert out["whys"][1] == {"text": "<img src=x onerror=alert(1)>", "bold": [], "kids": 0}
    assert len(out["whys"]) == 3


def test_the_library_s_picks_lead_the_one_grid() -> None:
    assert _run("leadOwned")["cards"] == ["Film 1", "Film 2"]


def test_the_library_s_picks_lead_whatever_the_server_names_first_and_through_a_narrowing() -> None:
    out = _run("leadHolds")
    assert out["before"] == ["Film 1", "Film 2"]
    assert "Funnier" in out["sent"]
    # A narrowing run's titles so far do not replace the answer it narrows.
    assert out["during"] == ["Film 1", "Film 2"]
    assert out["after"] == ["Film 1", "Film 2"]


def test_a_running_poll_draws_the_titles_checked_so_far_with_outlines_after_them() -> None:
    out = _run("partial")
    assert out["none"] == [[], 10]
    assert out["one"] == [["Film 2"], 9, ["It is funny."]]
    assert out["three"] == [["Film 1", "Film 2", "Film 3"], 7]
    # Done: the outlines go.
    assert out["done"] == [["Film 1", "Film 2", "Film 3"], 0]


THINKING = {
    "kicker": "AI is looking",
    "outlines": 10,
    "busy": "true",
    "off": "",
    "again": "",
    "chip": "Thinking",
}


def _given_up(seen: dict) -> None:
    """The wait is over: no outlines, no "Thinking", one line that says so, and Try again."""
    assert seen["outlines"] == 0
    assert seen["busy"] == "false"
    assert seen["chip"] == "Ask AI"
    assert "couldn’t answer" in seen["off"]
    assert seen["again"] == "Try again"


def test_a_run_that_stays_running_is_given_up_at_the_deadline_and_ended_on_the_server() -> None:
    out = _run("forever")
    assert out["start"] == THINKING
    assert out["before"] == THINKING
    _given_up(out["after"])
    assert out["stopped"] == ["/api/search/assist/runs/7"]
    # Nothing is polled once it is given up.
    assert out["polledAfter"] == 0


def test_the_deadline_counts_the_tries_while_the_run_before_stops_as_well() -> None:
    out = _run("stopping")
    assert out["tries"] == 40
    assert out["before"] == THINKING
    _given_up(out["after"])


def test_a_run_that_fails_or_is_stopped_elsewhere_ends_the_wait_at_once() -> None:
    out = _run("failed")
    _given_up(out["failed"])
    _given_up(out["cancelled"])


def test_try_again_asks_the_question_anew() -> None:
    out = _run("failed")
    assert out["asked"] == ["comedy movies from 2026", "comedy movies from 2026"]
    assert out["again"] == THINKING
