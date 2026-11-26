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


def test_similar_to_names_the_title_and_shows_tmdbs_like_it_before_the_ai_is_asked() -> None:
    out = _run("similar")
    assert "/api/watch/similar?q=90%20days%20fiance" in out["asked"]
    # The plain search is for the title named, not for the words around it.
    assert "/api/watch/search?q=90%20days%20fiance" in out["asked"]
    offered = out["offered"]
    assert offered["head"].startswith("Because you like 90 Day Fiancé")
    assert offered["face"] is True
    assert offered["cards"] == ["Love Is Blind", "Married at First Sight"]


def test_while_the_ai_looks_tmdbs_titles_stay_with_outlines_after_them_and_each_step_is_said() -> None:
    out = _run("similar")
    thinking = out["thinking"]
    assert thinking["head"] == "Because you like 90 Day Fiancé | Reading what you watch…"
    assert thinking["cards"] == ["Love Is Blind", "Married at First Sight"]
    assert thinking["outlines"] > 0
    assert out["choosing"].endswith("Choosing titles…")
    assert out["checking"].endswith("Checking them on TMDB…")


def test_the_ais_picks_merge_in_marked_once_each_and_never_the_title_named() -> None:
    answered = _run("similar")["answered"]
    assert answered["cards"] == [
        "*Nine Locks (The vault opens first time.)",
        "Love Is Blind",
        "*Married at First Sight (The same leap of faith.)",
        "*<b>Thunder</b> (<img src=x onerror=alert(1)>)",
    ]
    assert answered["groups"] == ["In your library", "Not in your library"]
    assert answered["outlines"] == 0


def test_a_title_nothing_names_leaves_the_ais_answer_alone() -> None:
    out = _run("similarUnnamed")
    assert out["offered"]["head"].startswith("Ask AI: “similar to zzz qqq”")
    assert out["answered"]["cards"] == [
        "*Nine Locks (The vault opens first time.)",
        "*<b>Thunder</b> (<img src=x onerror=alert(1)>)",
    ]


def test_without_the_ai_the_title_named_and_its_like_show_with_why_there_are_no_ai_picks() -> None:
    out = _run("similarNoAi")
    for who in ("admin", "viewer"):
        assert out[who]["cards"] == ["Love Is Blind"]
        assert out[who]["enter"] == 0
    assert out["admin"]["note"].startswith("AI picks: AI needs an OpenAI key.")
    assert out["admin"]["link"] == "/settings#intg-openai"
    assert out["viewer"]["note"] == "AI picks: AI isn’t available right now."
    assert out["viewer"]["link"] is None


def test_no_title_having_the_words_is_said_only_once_the_ai_is_done() -> None:
    out = _run("pendingQuiet")
    assert out["thinking"] == [False, False]
    assert out["answered"] == ["No title has these words in its name.", True]


def test_each_way_an_answer_fails_has_a_way_on() -> None:
    out = _run("similarErrors")
    assert out["keyAdmin"]["head"] == "OpenAI refused the key | Check it in the OpenAI card."
    assert "Open the OpenAI card -> /settings#intg-openai" in out["keyAdmin"]["ways"]
    assert out["keyViewer"]["head"].startswith("AI isn’t available right now")
    assert not any("settings" in w for w in out["keyViewer"]["ways"])
    assert out["offAdmin"]["head"].startswith("AI needs an OpenAI key")
    assert out["late"]["head"].startswith("The answer didn’t arrive")
    assert out["nothing"]["ways"][0] == "a reality dating show"
    for way in out.values():
        assert "Search for “90 days fiance”" in way["ways"]
        assert way["search"] == "90 days fiance"
        # What TMDB found like it stays.
        assert way["cards"] == ["Love Is Blind"]


def test_an_ai_picks_why_is_in_its_preview_and_goes_to_its_page() -> None:
    out = _run("aiWhy", "watch_preview_harness.js")
    assert out["early"] == "AI pickTense and kind, as you like them."
    assert out["late"] == out["early"]
    assert out["why"] == "Tense and kind, as you like them."


def test_an_ai_picks_page_says_why_and_a_title_of_no_pick_says_nothing() -> None:
    out = _run("aiWhy", "watch_detail_harness.js")
    assert out["why"].endswith("Slow and strange, as you like them.")
    assert out["plain"] is False
