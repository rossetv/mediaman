"""The page a download was asked for on, of one Radarr or Sonarr will not import without a
person, run under node against a fake DOM."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.unit.web.test_download_page_js import _run

STYLES = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css"


def test_it_says_it_needs_attention_with_no_bar_and_no_colour_of_almost_ready() -> None:
    seen = _run("attention")
    almost = _run("run")["almost"]
    assert (seen["step"], seen["bar"], seen["said"]) == (
        "Needs attention",
        False,
        "Needs attention",
    )
    assert seen["colour"] < almost["colour"]


def test_a_download_being_found_another_copy_of_is_not_called_searching() -> None:
    seen = _run("finding")
    assert (seen["bar"], seen["said"]) == (False, "Finding another copy")
    assert "Finding another copy" in seen["words"]
    assert "Searching" not in seen["words"]


def test_it_does_not_promise_a_stuck_download_carries_on_by_itself() -> None:
    seen = _run("attention")
    assert "carries on" not in seen["words"]
    assert seen["held"] is True


def test_it_says_another_copy_will_be_looked_for_when_mediaman_will_look() -> None:
    assert "another copy" in _run("attention", json.dumps({"retrying": True}))["words"]
    assert "another copy" not in _run("attention", json.dumps({"retrying": False}))["words"]


def test_an_admin_is_told_where_to_fix_it_and_anyone_else_who_will() -> None:
    hint = "Fix it in Radarr: Activity → Queue"
    admin = _run("attention", json.dumps({"attention_hint": hint}))["words"]
    other = _run("attention")["words"]
    assert hint in admin
    assert "Activity" not in other and "admin" in other


def test_its_step_is_drawn_in_the_warning_colour_and_does_not_pulse() -> None:
    css = (STYLES / "_download_page.css").read_text("utf-8")
    rule = re.search(r"\.share-steps\.is-held \.is-now i::after \{([^}]*)\}", css)
    assert rule is not None
    assert "var(--warning)" in rule[1] and "animation: none" in rule[1]
