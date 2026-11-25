"""On a short screen the section strip scrolls with the settings page, leaving room beside the save bar.

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
SHORT = "@media (max-width: 960px) and (max-height: 500px) {"


def test_the_section_strip_scrolls_away_on_a_short_screen_only() -> None:
    assert CSS.count(SHORT) == 1
    at = CSS.index(SHORT)
    body = CSS[at + len(SHORT) : CSS.index("\n}", at)]
    # relative, as the strip's faded edges are placed against it
    assert re.search(r"\.setg-rail \{[^}]*position: relative; top: auto;", body)
    # over every rule that sticks it, and the only one to let it go
    assert at > CSS.rindex("position: sticky")
    # with nothing pinned but the top bar, what is scrolled into view stops below that
    assert "html:has(.setg-rail) { scroll-padding-top: 60px; }" in body
    assert at > CSS.index("html:has(.setg-pg) { scroll-padding-top: calc(var(--nav-h) + 65px")
