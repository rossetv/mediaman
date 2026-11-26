"""A sideways row of cards leaves a vertical scroll to the page, read from the Watch stylesheet."""

from __future__ import annotations

import re
from pathlib import Path

SHEET = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch.css"


def _declarations(selector: str) -> dict[str, str]:
    """The last value of each property *selector* is given, in rules outside any `@media`."""
    css = re.sub(r"/\*.*?\*/", "", SHEET.read_text(encoding="utf-8"), flags=re.S)
    css = re.sub(r"@media ([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}", "", css)
    found: dict[str, str] = {}
    for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in [s.strip() for s in re.split(r",(?![^()]*\))", head)]:
            for name, value in re.findall(r"([-\w]+)\s*:\s*([^;]+)", body):
                found[name] = value.strip()
    return found


def test_a_row_of_cards_cannot_take_a_vertical_scroll_from_the_page() -> None:
    # A card's tap areas reach past the row's bottom; were the row scrollable on y, the wheel
    # over it would move it those few pixels and never the page.
    row = _declarations(".watch-row__scroll")
    assert row.get("overflow-x") == "auto"
    assert row.get("overflow-y") == "hidden"
