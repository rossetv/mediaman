"""The Downloads page of a download Radarr or Sonarr will not import without a person, run under
node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "downloads_harness.js"


@pytest.fixture(scope="module")
def page() -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), "needsAttention"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("card", ["hero", "row"])
def test_an_admin_reads_what_is_wrong_and_where_to_fix_it(page, card) -> None:
    shown = page["admin"][card]
    assert shown["pill"] == "Needs attention"
    assert shown["hint"] == "Review file name or manually import"
    assert shown["fix"] == "Fix it in Sonarr: Activity → Queue"


@pytest.mark.parametrize("card", ["hero", "row"])
def test_a_viewer_reads_only_that_it_needs_attention(page, card) -> None:
    shown = page["viewer"][card]
    assert shown["pill"] == "Needs attention"
    assert (shown["hint"], shown["fix"]) == (None, None)


@pytest.mark.parametrize("card", ["hero", "row"])
def test_it_shows_no_full_bar_and_its_episodes_say_why(page, card) -> None:
    shown = page["admin"][card]
    assert shown["bar"] is False
    assert shown["pct"] in (None, "—")
    assert shown["episode"] == "Needs attention"


@pytest.mark.parametrize("card", ["hero", "row"])
@pytest.mark.parametrize("who", ["admin", "viewer"])
def test_one_mediaman_will_retry_says_so_in_place_of_where_to_fix_it(page, card, who) -> None:
    assert page["retrying"][who][card]["fix"] == "mediaman will look for another copy"
