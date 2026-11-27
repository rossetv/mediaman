"""With reduced motion, the Settings pane's list and sections only fade, at the short duration.

Read from the stylesheet, since no layout engine runs here.
"""

from __future__ import annotations

import re

from tests.unit.web._css import style, text, token

SHEET = "_settings_pg.css"
REDUCED = "(prefers-reduced-motion: reduce)"


def _seconds(value: str) -> list[float]:
    """Each time of a comma-separated list, with var() read from the tokens."""
    value = re.sub(r"var\((--[\w-]+)\)", lambda m: token(m[1]), value.replace("!important", ""))
    times = re.findall(r"([\d.]+)(m?s)\b", value)
    return [float(n) / (1000 if unit == "ms" else 1) for n, unit in times]


def _animates(name: str) -> set[str]:
    """What the keyframes called *name* change."""
    found = re.search(
        rf"@keyframes {re.escape(name)}\s*\{{((?:[^{{}}]*\{{[^}}]*\}})*)\s*\}}", text(SHEET)
    )
    assert found, name
    return {prop.strip() for prop in re.findall(r"([\w-]+)\s*:", found[1])}


def _fades_only(selector: str) -> None:
    rule = style(selector, SHEET, REDUCED)
    assert _animates(rule["animation-name"]) == {"opacity"}
    assert max(_seconds(rule["animation-duration"])) <= _seconds(token("--dur-fast"))[0]


def test_reduced_motion_moves_neither_side_of_the_pane_and_fades_at_the_short_duration() -> None:
    fast = _seconds(token("--dur-fast"))[0]
    for side in (".stx__nav", ".stx__body", ".stx.is-in > .stx__nav", ".stx.is-in > .stx__body"):
        rule = style(side, SHEET, REDUCED)
        assert rule["transform"].split()[0] == "none", side
        assert "transform" not in rule["transition-property"], side
        assert max(_seconds(rule["transition-duration"])) <= fast, side


def test_a_section_arriving_beside_the_list_fades_and_does_not_rise() -> None:
    _fades_only(".stx-sec.is-arriving")


def test_the_save_bar_fades_in_at_the_short_duration() -> None:
    _fades_only(".stx-save")
