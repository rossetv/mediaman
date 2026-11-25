"""What the storage panel's stylesheet has to hold, read as rules."""

from __future__ import annotations

import re
from pathlib import Path

SHEET = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch_space.css"


def _inset(selector: str) -> list[float]:
    css = re.sub(r"/\*.*?\*/", "", SHEET.read_text(encoding="utf-8"), flags=re.S)
    block = re.search(re.escape(selector) + r" \{([^{}]*)\}", css)
    assert block, selector
    inset = re.search(r"inset: ([^;]+);", block.group(1))
    assert inset, selector
    return [float(v.removesuffix("px")) for v in inset.group(1).split()]


def test_the_history_link_in_the_scan_reason_reaches_44px_without_covering_the_button() -> None:
    vertical, horizontal = _inset(".space__scan-status a::after")
    # An inline link's box is its text's, about 15px tall at 12px: 15 either side makes 45.
    assert -vertical >= 15
    # The button is 12px off: the reach stays inside half of that.
    assert -horizontal <= 6


def _declarations(selector: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", SHEET.read_text(encoding="utf-8"), flags=re.S)
    block = re.search(re.escape(selector) + r" \{([^{}]*)\}", css)
    assert block, selector
    return block.group(1)


def test_the_meter_shows_its_bar_at_every_width_whatever_marks_stand_beside_it() -> None:
    css = re.sub(r"/\*.*?\*/", "", SHEET.read_text(encoding="utf-8"), flags=re.S)
    hidden = [
        selector
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if re.search(r"space-meter__bar\s*(,|$)", selector.strip()) and "display: none" in body
    ]
    assert hidden == []


def test_only_the_bar_s_two_outer_ends_are_round() -> None:
    # every part's corners are 1px, and only the first part's start and the last part's end
    # (or the reserved part's end) take the full round, so the corners between parts stay square
    assert "border-radius: 1px;" in _declarations(".space-seg::before")
    assert "border-radius: 1px 11px 11px 1px;" in _declarations(".space-bar__rest")
    first = _declarations(".space-seg--first::before")
    last = _declarations(".space-seg--last::before")
    assert "start-start-radius: 11px" in first and "end-start-radius: 11px" in first
    assert "start-end-radius: 11px" in last and "end-end-radius: 11px" in last
