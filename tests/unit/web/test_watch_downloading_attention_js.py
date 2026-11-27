"""The Watch page's Downloading row of a download Radarr or Sonarr will not import without a
person, run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.unit.web._css import style

HARNESS = Path(__file__).parent / "js" / "watch_downloading_harness.js"
_WHY = "Single episode file contains all episodes in seasons. Review file name or manually import"


@pytest.fixture(scope="module")
def card() -> dict:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["needsAttention"]


def test_an_admin_reads_that_it_needs_attention_and_why(card) -> None:
    assert card["told"]["line"] == "Needs attention · " + _WHY
    assert card["told"]["title"] == _WHY + " · Fix it in Sonarr: Activity → Queue"


_ADMIN = "Won’t finish until an admin sorts it out"


def test_a_viewer_reads_that_it_needs_attention_and_waits_for_an_admin(card) -> None:
    assert card["viewer"]["line"] == "Needs attention"
    assert card["viewer"]["fix"] == _ADMIN
    assert card["viewer"]["title"] == _ADMIN


def test_it_has_no_bar_of_progress_but_the_warning_one(card) -> None:
    classes = set(card["told"]["classes"].split()) - {"watch-card", "dw"}
    assert classes == {"is-looking", "is-stuck"}
    assert card["told"]["fill"] == "0"


def test_an_admin_reads_where_to_fix_it_on_the_card_itself_not_only_on_hover(card) -> None:
    assert card["told"]["fix"] == "Fix it in Sonarr: Activity → Queue"


def test_one_mediaman_will_try_again_says_so_to_an_admin_and_a_viewer_alike(card) -> None:
    said = "mediaman will look for another copy"
    assert card["retried"]["fix"] == said
    assert card["retried"]["title"] == _WHY + " · " + said
    assert card["viewerRetried"]["fix"] == said
    assert card["viewerRetried"]["line"] == "Needs attention"
    assert card["viewerRetried"]["title"] == said


def test_a_card_that_is_stuck_has_no_full_bar_that_reads_as_done() -> None:
    assert style(".dw.is-stuck .dw__bar i", "_watch_downloading.css")["opacity"] == "0"


def test_where_to_fix_it_is_plain_text_not_the_colour_of_a_link() -> None:
    for selector, sheet in (
        (".dl-attention-hint", "_dl.css"),
        (".dw__fix", "_watch_downloading.css"),
    ):
        colour = style(selector, sheet)["color"]
        assert "--warning" not in colour and "--accent" not in colour, selector


def test_a_stuck_card_gives_what_happens_next_two_lines_and_grows_to_hold_them() -> None:
    fix = style(".dw.is-stuck .dw__fix", "_watch_downloading.css")
    link = style(".dw__link", "_watch_downloading.css")
    assert fix["-webkit-line-clamp"] == "2" and fix["white-space"] == "normal"
    assert "min-height" in link and "height" not in link
