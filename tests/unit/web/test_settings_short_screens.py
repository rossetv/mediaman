"""With reduced motion, the Settings pane's list and sections only fade, at the short duration.

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
REDUCED = "@media (prefers-reduced-motion: reduce) {"


def test_reduced_motion_moves_nothing_in_the_pane_and_fades_at_the_short_duration() -> None:
    blocks = [
        CSS[m.end() : CSS.index("\n}", m.end())] for m in re.finditer(re.escape(REDUCED), CSS)
    ]
    pane = next(b for b in blocks if ".stx__nav" in b)
    assert "transform: none" in pane and "var(--dur-fast)" in pane
    assert ".stx-sec.is-arriving { animation: none; }" in pane
    bar = next(b for b in blocks if ".stx-save" in b)
    assert "var(--dur-fast)" in bar
