"""The /downloads queue rows' layout around a row's abandon button, read from _dl.css."""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).parents[3] / "src/mediaman/web/static/css/_dl.css").read_text()


def _media(query: str) -> str:
    """The body of every ``@media <query>`` block, joined."""
    bodies = []
    for start in [m.end() for m in re.finditer(re.escape(f"@media {query}") + r"\s*\{", CSS)]:
        depth, i = 1, start
        while depth:
            depth += {"{": 1, "}": -1}.get(CSS[i], 0)
            i += 1
        bodies.append(CSS[start : i - 1])
    return "\n".join(bodies)


def _top_level() -> str:
    return re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", CSS)


def _value(text: str, selector: str, prop: str) -> str | None:
    """*prop* of the last rule in *text* whose selector list holds *selector*."""
    found = None
    for sels, body in re.findall(
        r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    ):
        if selector in [s.strip() for s in sels.split(",")]:
            m = re.search(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", body)
            found = m.group(1).strip() if m else found
    return found


def _tracks(columns: str | None) -> int:
    assert columns, "no grid-template-columns"
    return len(re.findall(r"minmax\([^)]*\)|calc\([^)]*\)|var\([^)]*\)|[^\s()]+", columns))


ROW = ".dl-compact > .dl-row"
WITH_BUTTON = ".dl-compact > .dl-row--has-abandon"
WIDE = _media("(min-width: 375px)")
NARROW = _media("(max-width: 374.98px)")


def test_a_rows_abandon_button_has_a_column_of_its_own_at_the_rows_end() -> None:
    plain = _value(_top_level(), ".dl-compact-row", "grid-template-columns")
    assert _tracks(_value(WIDE, WITH_BUTTON, "grid-template-columns")) == _tracks(plain) + 1
    assert _value(_top_level(), ".dl-compact .dl-row-abandon", "grid-area") == "1 / -2"


def test_on_a_narrow_phone_the_button_is_the_only_extra_column() -> None:
    assert _value(NARROW, WITH_BUTTON, "grid-template-columns") is None
    assert _tracks(_value(NARROW, ROW, "grid-template-columns")) == 3
    # the percentage and time left go under the title, out of the button's column
    right = _value(NARROW, ".dl-compact > .dl-row--has-abandon .dl-compact-right", "grid-area")
    assert right is not None and right.split("/")[1].strip() == "2"
