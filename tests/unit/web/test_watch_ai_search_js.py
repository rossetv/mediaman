"""The AI in Watch search: "similar to" a title, the steps of an answer, its picks on the
cards every row has, and what is said when it goes wrong. Run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

JS = Path(__file__).parent / "js"


def _run(scenario: str, harness: str = "watch_search_harness.js") -> dict:
    result = subprocess.run(
        ["node", str(JS / harness), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_similar_to_finds_the_title_it_names_and_holds_the_answers_place_under_it() -> None:
    out = _run("similar")
    assert "/api/watch/similar?q=90%20days%20fiance" in out["asked"]
    # The plain search is for the title named, not for the words around it.
    assert "/api/watch/search?q=90%20days%20fiance" in out["asked"]
    waiting = out["waiting"]
    assert waiting["head"].startswith("Ask AI: “similar to 90 days fiance”")
    assert (waiting["because"], waiting["face"]) == ("90 Day Fiancé", True)
    # Outlines hold the answer's place; nothing is shown that the AI did not pick.
    assert (waiting["cards"], waiting["outlines"]) == ([], 10)


def test_similar_to_is_asked_as_any_question_with_the_title_it_names() -> None:
    assert _run("similar")["sent"] == [
        {"q": "similar to 90 days fiance", "like": {"tmdb_id": 70, "kind": "tv", "title": "90 Day Fiancé"}}
    ]


def test_a_question_naming_a_title_is_asked_once_that_title_is_known_and_once_only() -> None:
    out = _run("similarWaits")
    like = {"tmdb_id": 70, "kind": "tv", "title": "90 Day Fiancé"}
    assert out["found"] == [0, [{"q": "similar to 90 days fiance", "like": like}]]
    # Found only in the library, or not at all, it is asked in words alone.
    assert out["held"] == [0, [{"q": "similar to 90 days fiance"}]]
    assert out["failed"] == [0, [{"q": "similar to 90 days fiance"}]]


def test_while_the_ai_looks_its_outlines_hold_the_place_and_each_step_is_said() -> None:
    out = _run("similar")
    thinking = out["thinking"]
    assert thinking["kicker"] == "AI is looking"
    assert thinking["head"] == "Shows like 90 Day Fiancé | Reading what you watch…"
    assert (thinking["cards"], thinking["outlines"]) == ([], 10)
    assert out["choosing"].endswith("Choosing titles…")
    assert out["checking"].endswith("Checking each one is real…")


def test_the_answer_is_one_grid_of_picks_ready_ones_first_never_the_title_named() -> None:
    answered = _run("similar")["answered"]
    assert answered["kicker"] == "AI picks"
    assert answered["head"] == "Reality romance worth your time | 3 titles · 1 ready to watch, 2 to download"
    assert answered["because"] == "90 Day Fiancé"
    assert answered["cards"] == [
        "*Nine Locks (The vault opens first time.)",
        "*Married at First Sight (The same leap of faith.)",
        "*<b>Thunder</b> (<img src=x onerror=alert(1)>)",
    ]
    assert answered["outlines"] == 0


def test_a_title_nothing_names_leaves_the_ais_answer_alone() -> None:
    out = _run("similarUnnamed")
    assert out["waiting"]["head"].startswith("Ask AI: “similar to zzz qqq”")
    assert out["waiting"]["because"] is None
    assert out["sent"] == [{"q": "similar to zzz qqq"}]
    assert out["answered"]["cards"] == [
        "*Nine Locks (The vault opens first time.)",
        "*<b>Thunder</b> (<img src=x onerror=alert(1)>)",
    ]


def test_without_the_ai_an_admin_is_told_what_it_needs_and_a_viewer_nothing() -> None:
    out = _run("similarNoAi")
    for who in ("admin", "noKey", "off", "viewer"):
        assert (out[who]["cards"], out[who]["enter"]) == ([], 0)
    assert out["admin"]["head"] == "OpenAI refused the key | Check it in the OpenAI card."
    assert out["noKey"]["head"].startswith("AI could answer this")
    for who in ("admin", "noKey"):
        assert out[who]["link"] == "/settings#intg-openai"
    # Switched off on purpose it is no news; a viewer has nothing to set.
    for who in ("off", "viewer"):
        assert (out[who]["shown"], out[who]["head"]) == (False, None)


def test_no_title_having_the_words_is_said_only_once_the_ai_is_done() -> None:
    out = _run("pendingQuiet")
    assert out["thinking"] is False
    assert out["answered"] == "No title has these words in its name."


def test_each_way_an_answer_fails_has_a_way_on() -> None:
    out = _run("similarErrors")
    assert out["keyAdmin"]["head"] == "OpenAI refused the key | Check it in the OpenAI card."
    assert "Open the OpenAI card -> /settings#intg-openai" in out["keyAdmin"]["ways"]
    assert out["keyViewer"]["head"].startswith("AI isn’t available right now")
    assert not any("settings" in w for w in out["keyViewer"]["ways"])
    assert out["offAdmin"]["head"].startswith("AI could answer this")
    assert out["late"]["head"].startswith("AI couldn’t answer just now")
    assert out["nothing"]["ways"][0] == "a reality dating show"
    for way in out.values():
        assert "Search for “90 days fiance”" in way["ways"]
        assert way["search"] == "90 days fiance"


def test_an_ai_picks_why_is_in_its_preview_and_goes_to_its_page() -> None:
    out = _run("aiWhy", "watch_preview_harness.js")
    assert out["early"] == "AI pickTense and kind, as you like them."
    assert out["late"] == out["early"]
    assert out["why"] == "Tense and kind, as you like them."


def test_an_ai_picks_page_says_why_and_a_title_of_no_pick_says_nothing() -> None:
    out = _run("aiWhy", "watch_detail_harness.js")
    assert out["why"].endswith("Slow and strange, as you like them.")
    assert out["plain"] is False
