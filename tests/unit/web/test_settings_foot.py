"""The save bar never covers the settings page's foot, and leaves no room above it.

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


def test_the_foot_and_what_is_scrolled_into_view_clear_the_bar_by_the_same_room() -> None:
    assert "html:has(.setg-pg .savebar.on) .foot { padding-bottom: var(--savebar-room); }" in CSS
    # no size of the bar has a room of its own
    assert re.findall(r"scroll-padding-bottom: ([^;]+);", CSS) == ["var(--savebar-room)"]


def test_room_for_the_bar_is_not_kept_above_the_foot() -> None:
    kept = re.findall(r"\.setg-shell \{[^}]*padding-bottom: (\d+)px", CSS)
    assert kept == ["64"]
