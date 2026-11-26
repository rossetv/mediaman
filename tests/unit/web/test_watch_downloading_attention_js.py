"""The Watch page's Downloading row of a download Radarr or Sonarr will not import without a
person, run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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


def test_a_viewer_reads_only_that_it_needs_attention(card) -> None:
    assert card["viewer"]["line"] == "Needs attention"
    assert card["viewer"]["title"] is None


def test_it_has_no_bar_of_progress_but_the_warning_one(card) -> None:
    classes = set(card["told"]["classes"].split()) - {"watch-card", "dw"}
    assert classes == {"is-looking", "is-stuck"}
    assert card["told"]["fill"] == "0"
