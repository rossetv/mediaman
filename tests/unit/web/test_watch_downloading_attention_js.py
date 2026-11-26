"""The Watch page's Downloading row of a download Radarr or Sonarr will not import without a
person, run under node against a fake DOM."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "watch_downloading_harness.js"
STYLES = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css"
_WHY = "Single episode file contains all episodes in seasons. Review file name or manually import"


@pytest.fixture(scope="module")
def card() -> dict:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["needsAttention"]


def test_an_admin_reads_that_it_needs_attention_and_why(card) -> None:
    assert card["told"]["line"] == "Needs attention · " + _WHY
    assert card["told"]["title"] == _WHY + " · Fix it in Sonarr: Activity → Queue"


def test_a_viewer_reads_only_that_it_needs_attention(card) -> None:
    assert card["viewer"]["line"] == "Needs attention"
    assert card["viewer"]["title"] is None


def test_it_has_no_bar_of_progress_but_the_warning_one(card) -> None:
    classes = set(card["told"]["classes"].split()) - {"watch-card", "dw"}
    assert classes == {"is-looking", "is-stuck"}
    assert card["told"]["fill"] == "0"


def test_an_admin_reads_where_to_fix_it_on_the_card_itself_not_only_on_hover(card) -> None:
    assert card["told"]["fix"] == "Fix it in Sonarr: Activity → Queue"
    assert card["viewer"]["fix"] is None


def test_one_mediaman_will_try_again_says_so_to_an_admin_and_a_viewer_alike(card) -> None:
    said = "mediaman will look for another copy"
    assert card["retried"]["fix"] == said
    assert card["retried"]["title"] == _WHY + " · " + said
    assert card["viewerRetried"]["fix"] == said
    assert card["viewerRetried"]["line"] == "Needs attention"
    assert card["viewerRetried"]["title"] == said


def _rule(sheet: str, selector: str) -> str:
    css = (STYLES / sheet).read_text("utf-8")
    found = re.search(re.escape(selector) + r" \{([^}]*)\}", css)
    assert found is not None, selector
    return found[1]


def test_a_card_that_is_stuck_has_no_full_bar_that_reads_as_done() -> None:
    assert "opacity: 0" in _rule("_watch_downloading.css", ".dw.is-stuck .dw__bar i")


def test_where_to_fix_it_is_plain_text_not_the_colour_of_a_link() -> None:
    assert "var(--warning)" not in _rule("_dl.css", ".dl-attention-hint")
    assert "var(--warning)" not in _rule("_watch_downloading.css", ".dw__fix")
