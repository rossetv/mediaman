"""The settings page keeps room under its last section for the save bar.

Read from the stylesheet, since no layout engine runs here.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = re.sub(
    r"/\*.*?\*/",
    "",
    (Path(__file__).parents[3] / "src/mediaman/web/static/css/_settings_pg.css").read_text(),
    flags=re.S,
)


def test_the_page_keeps_room_under_its_last_section_for_the_bar() -> None:
    # Never less than the bar's own height, which savebar.js measures, whatever it says.
    (room,) = re.findall(r"\.setg-shell \{[^}]*padding-bottom: ([^;]+);", CSS)
    measured = re.fullmatch(r"max\(140px, calc\(var\(--savebar-h, 0px\) \+ (\d+)px\)\)", room)
    assert measured and int(measured[1]) >= 16
    (above,) = re.findall(r"scroll-padding-bottom: ([^;]+);", CSS)
    assert "var(--savebar-h" in above and "var(--savebar-below)" in above
