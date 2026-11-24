"""What stands above the list's rows is as tall as the skeleton says, whatever the library holds."""

from __future__ import annotations

import re
from pathlib import Path

CSS = (
    Path(__file__).parents[5] / "src" / "mediaman" / "web" / "static" / "css" / "_watch_list.css"
).read_text()


def _rules(query: str) -> str:
    """The rules of every @media block whose query is *query*."""
    return "\n".join(
        re.findall(r"@media " + re.escape(query) + r" \{\n((?:(?!\}\n).*\n)*)\}\n", CSS)
    )


def test_a_header_stays_on_the_one_line_the_skeleton_draws() -> None:
    (rule,) = re.findall(r"\n\.wl-table thead th \{([^}]*)\}", CSS)
    assert "white-space: nowrap;" in rule


def test_the_summary_takes_two_lines_wherever_the_skeleton_has_two() -> None:
    (query,) = re.findall(
        r"@media (\S+ \S+) \{\n(?:  \.wl-summary.*\n)?  \.wl-skel__summary \{ margin-bottom", CSS
    )
    width = re.search(r"max-width: (\d+)px", _rules(query).split(".wl-skel__summary")[0])
    # The shortest summary is some 390px long.
    assert width and int(width[1]) <= 380


def test_the_sort_is_under_the_pills_wherever_the_skeleton_has_it_there() -> None:
    (query,) = re.findall(
        r"@media ([^{]+) \{\n(?:  \.wl-sort.*\n)?  \.wl-skel__bar \{ margin-bottom: 63px", CSS
    )
    assert re.search(r"\.wl-sort \{[^}]*flex-basis: 100%", _rules(query))
