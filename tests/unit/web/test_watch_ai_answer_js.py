"""An AI answer in Watch search: it lands without moving what is on the page, its cards say
what the library's own say, the keys reach them, and where the AI is off a search that would
have asked it says why. Run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

JS = Path(__file__).parent / "js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(JS / "watch_search_harness.js"), scenario],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_answer_keeps_the_cards_already_drawn_in_their_places() -> None:
    out = _run("answerKeeps")
    assert out["kept"] == [True, True]
    assert out["head"] == 0
    assert out["cards"][:2] == [
        "Love Is Blind",
        "*Married at First Sight (The same leap of faith.)",
    ]


def test_a_title_downloaded_for_the_library_is_grouped_with_it_and_not_badged() -> None:
    out = _run("heldByDownload")
    assert out["groups"] == ["In your library", "Not in your library"]
    assert out["badges"] == []


def test_an_ai_pick_of_a_show_in_the_library_says_its_seasons() -> None:
    assert _run("aiShowMeta")["sub"] == "TV · 2 seasons"


def test_a_bare_like_is_searched_as_typed_and_similar_to_searches_both() -> None:
    out = _run("likeWords")
    assert not any("similar" in u for u in out["bare"])
    assert "/api/watch/search?q=like crazy" in out["bare"]
    for q, named in (("similar to harrow lane", "harrow lane"), ("something like crazy", "crazy")):
        asked = out["similar" if named == "harrow lane" else "something"]
        assert f"/api/watch/similar?q={named}" in asked
        assert {f"/api/watch/search?q={named}", f"/api/watch/search?q={q}"} <= set(asked)


def test_where_the_ai_is_off_a_question_says_why_and_not_to_check_the_spelling() -> None:
    out = _run("offSaid")
    assert out["admin"]["note"].startswith("AI needs an OpenAI key.")
    assert out["admin"]["link"] == "/settings#intg-openai"
    assert out["viewer"]["note"].startswith("AI isn’t available right now.")
    assert out["viewer"]["link"] is None
    for who in ("admin", "viewer"):
        assert "spelling" not in out[who]["none"]


def test_without_tmdb_similar_to_searches_the_library_for_the_title_and_says_why() -> None:
    out = _run("offSaid")["noTmdb"]
    assert "/api/watch/search?q=harrow lane" in out["searched"]
    assert out["note"].startswith("AI needs TMDB.")
    assert out["link"] == "/settings#setg-integrations"


def test_return_on_a_title_typed_in_full_opens_it_and_asks_nothing() -> None:
    out = _run("exactReturn")
    assert out["rowPicked"] is False
    assert (out["opened"], out["asked"]) == (["movie-5"], 0)


def test_the_arrow_keys_reach_the_titles_of_an_answer() -> None:
    out = _run("arrowCards")
    assert (out["moved"], out["on"], out["active"]) == (True, "movie-1", True)


def test_the_veil_behind_the_open_sheet_can_still_be_pressed() -> None:
    out = _run("veilPressed")
    assert (out["open"], out["inert"], out["hidden"]) == (True, False, "true")


def test_no_step_or_line_a_viewer_reads_names_tmdb() -> None:
    out = _run("similar")
    said = [out["choosing"], out["checking"], out["foot"], _run("similarNoAi")["viewer"]["head"]]
    assert not [line for line in said if "TMDB" in line]
