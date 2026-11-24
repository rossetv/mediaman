"""What the emailed download page does when Download is pressed and while it waits."""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest

HARNESS = Path(__file__).parent / "js/download_page_harness.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def answers() -> dict:
    return _run("answers")


def test_a_title_already_on_its_way_gets_the_progress_card_and_not_a_failure(answers: dict) -> None:
    seen = answers["queued"]

    assert seen["card"] and not seen["content"]
    assert "already on its way" in seen["hint"]
    assert seen["result"] is None


def test_a_title_already_on_its_way_promises_the_email_the_answer_says_is_to_come(
    answers: dict,
) -> None:
    assert "notified by email" in answers["queuedMailed"]["hint"]
    assert "notified by email" not in answers["queued"]["hint"]


@pytest.mark.parametrize(
    ("answer", "said"),
    [("inLibrary", "Already in your library"), ("tracked", "Already tracked by Sonarr")],
)
def test_a_title_already_there_is_said_where_the_button_was_and_is_no_failure(
    answers: dict, answer: str, said: str
) -> None:
    seen = answers[answer]

    assert said in seen["result"] and not seen["error"]
    assert seen["button"] is None and seen["content"]
    assert seen["focus"] == "result"


@pytest.mark.parametrize("answer", ["unanswered", "down", "limited"])
def test_a_failure_worth_retrying_keeps_the_button_and_the_focus(
    answers: dict, answer: str
) -> None:
    seen = answers[answer]

    assert "Download failed" in seen["result"] and seen["error"]
    assert seen["button"] == "Try again" and seen["busy"] is None
    assert seen["focus"] == "button"


def test_a_link_that_cannot_work_again_offers_no_retry(answers: dict) -> None:
    for answer, said in (("expired", "Download failed"), ("used", "already been used")):
        seen = answers[answer]
        assert said in seen["result"] and seen["error"]
        assert seen["button"] is None
        assert seen["focus"] == "result"


_BUSY = {
    "label": "Adding…",
    "busy": "true",
    "focused": True,
    "spinner": True,
    "minWidth": "132px",
}


def test_a_refusal_that_gives_no_reason_does_not_say_to_try_again(answers: dict) -> None:
    seen = answers["wordless"]

    assert seen["error"] and seen["button"] is None
    assert "again" not in seen["result"].replace("Download failed", "")


def test_pressing_keeps_the_focus_sends_once_and_can_be_tried_again() -> None:
    out = _run("retry")

    assert out["first"] == _BUSY
    assert out["failed"]["posted"] == 1
    assert out["second"] == _BUSY
    assert out["done"]["posted"] == 2
    assert out["done"]["card"] and not out["done"]["content"]


def test_the_card_built_after_the_press_has_what_the_server_draws_and_takes_the_focus() -> None:
    assert _run("card") == {
        "headings": ["Film"],
        "focused": "H1",
        "pills": ["TV"],
        "state": "Searching",
        "links": ["/"],
    }


def test_the_episodes_sit_in_the_card_s_text_column_and_open_from_a_button() -> None:
    out = _run("episodes")

    assert out["tag"] == "BUTTON" and out["inInfo"]
    assert out["pills"] == ["Queued", "Searching"]
    assert out["closed"] == {"expanded": "false", "shown": False}
    assert out["open"] == {"expanded": "true", "shown": True}
    assert out["after"] == {
        "expanded": "true",
        "shown": True,
        "rows": ["Ready", "Searching", "Queued"],
    }
    assert out["closedAgain"] == {"expanded": "false", "shown": False}


def test_the_card_comes_in_and_its_parts_open_and_close() -> None:
    out = _run("motion")

    assert out["enters"]
    assert sorted(out["starts"]) == [
        "reveal dl-hero-ep-summary",
        "reveal ep-toggle",
        "reveal progress-wrap",
    ]
    assert out["goesOn"] == []
    assert sorted(out["ends"]) == [
        "collapse progress-wrap",
        "collapseAway dl-hero-ep-summary",
        "collapseAway dl-hint",
        "collapseAway ep-toggle",
        "reveal dl-result",
    ]


def test_what_closes_keeps_its_figures_and_gives_its_focus_to_the_heading() -> None:
    out = _run("motion")

    assert "3 GB of 9 GB" in out["figures"]
    assert out["left"] == 0 and out["focus"] == "H1"


def test_the_links_are_a_finger_s_reach_on_any_touch_screen() -> None:
    css = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_download_page.css"
    coarse = re.search(r"@media \(pointer: coarse\) \{(.*?)\n\}", css.read_text(), re.S)

    assert coarse and re.search(
        r"\.dl-nav \.nav-brand,\s*\.dl-footer a \{[^}]*min-height: 44px", coarse[1]
    )


def test_a_service_that_does_not_answer_leaves_the_last_progress_drawn() -> None:
    assert _run("episodes")["unanswered"] == "Downloading"


def test_a_title_that_becomes_ready_keeps_its_card_and_its_link() -> None:
    out = _run("ready")

    assert len(out["status"]) == 1 and "Ready to watch!" in out["status"][0]
    assert out["card"] and out["links"] == 1
    assert not out["progress"]


def test_the_answer_shows_where_the_button_is_and_is_read_out() -> None:
    page = (
        Path(__file__).resolve().parents[3] / "src/mediaman/web/templates/download.html"
    ).read_text()
    button, footer = page.index('id="btn-download"'), page.index('class="dl-footer"')

    assert button < page.index('id="download-result" class="dl-result" role="alert"') < footer


def test_a_long_word_wraps_on_the_page_and_on_its_card() -> None:
    css = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_download_page.css"

    assert ".dl-content,\n#dl-hero-wrapper { overflow-wrap: anywhere; }" in css.read_text()


def test_the_built_card_says_what_the_server_drawn_one_says() -> None:
    templates = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(templates), autoescape=True)
    title = {"title": "Film", "media_type": "movie", "state": "searching", "progress": 0}
    item = {**title, "notify_by_email": True, "download_state": "queued"}
    page = env.get_template("download.html").render(state="confirm", item=item, hero_item=title)
    drawn = re.search(r'id="dl-hint"[^>]*>(.*?)</div>', page, re.S)

    assert drawn
    words = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", drawn[1])).split())
    assert " ".join(_run("hint")["hint"].split()) == words
