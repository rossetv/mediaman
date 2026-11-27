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


def test_the_answer_lands_where_its_outlines_stood_and_keeps_the_cards_already_drawn() -> None:
    out = _run("answerKeeps")
    assert out["thinking"] == ["outline"] * 10
    assert out["checking"] == ["card"] + ["outline"] * 9
    assert out["answered"] == ["card"] * 3
    assert out["kept"] is True


def test_a_title_downloaded_for_the_library_is_ready_to_watch_and_stands_with_it() -> None:
    out = _run("heldByDownload")
    assert out["cards"] == ["*Married at First Sight", "*Love Is Blind"]
    assert out["badges"] == ["Ready", "Download"]
    assert out["line"] == "2 titles · 1 ready to watch, 1 to download"


def test_an_ai_pick_of_a_show_in_the_library_says_its_seasons() -> None:
    assert _run("aiShowMeta")["sub"] == "TV · 2 seasons"


def test_a_library_pick_keeps_its_keep_and_its_leaving_badge() -> None:
    leaver = _run("aiShowMeta")["leaver"]
    assert leaver["keep"] == 1 and "watch-badge--leaving" in leaver["badges"]


def test_a_bare_like_is_searched_as_typed_and_similar_to_searches_both() -> None:
    out = _run("likeWords")
    assert not any("similar" in u for u in out["bare"])
    assert "/api/watch/search?q=like crazy" in out["bare"]
    for q, named in (("similar to harrow lane", "harrow lane"), ("something like crazy", "crazy")):
        asked = out["similar" if named == "harrow lane" else "something"]
        assert f"/api/watch/similar?q={named}" in asked
        assert {f"/api/watch/search?q={named}", f"/api/watch/search?q={q}"} <= set(asked)


def test_similar_to_asks_tmdb_for_the_title_alone_and_outlives_the_sentence_failing() -> None:
    out = _run("similarSentence")
    assert out["tmdb"] == ["/api/watch/search/tmdb?q=harrow lane"]
    assert out["titles"] == ["Orchard Keepers", "Orchard Road"]
    assert out["failed"] == 0


def test_where_the_ai_is_set_up_and_fails_a_question_tells_an_admin_why() -> None:
    out = _run("offSaid")
    assert out["refused"]["note"].startswith("OpenAI refused the key |")
    assert out["refused"]["link"] == "/settings#intg-openai"
    assert "spelling" not in out["refused"]["none"]


def test_with_no_key_an_admin_is_nudged_to_the_openai_card_and_a_viewer_told_nothing() -> None:
    out = _run("offSaid")
    assert out["noKey"]["note"].startswith("AI could answer this |")
    assert out["noKey"]["link"] == "/settings#intg-openai"
    for who in ("off", "viewer"):
        assert (out[who]["note"], out[who]["link"]) == (None, None)


def test_without_tmdb_similar_to_searches_the_library_for_the_title_and_says_why() -> None:
    out = _run("offSaid")["noTmdb"]
    assert "/api/watch/search?q=harrow lane" in out["searched"]
    assert out["note"].startswith("AI needs TMDB |")


def test_without_tmdb_an_admin_is_offered_to_set_it_up_once_whatever_the_library_has() -> None:
    out = _run("offSaid")
    assert (out["noTmdb"]["setups"], out["noTmdbFound"]["setups"]) == (1, 1)


def test_return_on_a_title_typed_in_full_opens_it_and_asks_nothing() -> None:
    out = _run("exactReturn")
    assert (out["opened"], out["asked"]) == (["movie-5"], 0)


def test_the_arrow_keys_reach_the_titles_of_an_answer() -> None:
    out = _run("arrowCards")
    assert (out["moved"], out["on"], out["active"]) == (True, "movie-1", True)


def test_the_veil_behind_the_open_sheet_can_still_be_pressed() -> None:
    out = _run("veilPressed")
    assert (out["open"], out["inert"], out["hidden"]) == (True, False, "true")


def test_no_step_or_line_a_viewer_reads_names_tmdb() -> None:
    out = _run("similar")
    said = [
        out["choosing"],
        out["checking"],
        out["foot"],
        _run("similarErrors")["keyViewer"]["head"],
    ]
    assert not [line for line in said if "TMDB" in line]
