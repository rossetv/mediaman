"""The Settings pane shows its section list and the section on show side by side only on a wide screen.

Read from the stylesheet, since no layout engine runs here; sections.js reads the same width.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3] / "src/mediaman/web/static"
CSS = re.sub(r"/\*.*?\*/", "", (ROOT / "css/_settings_pg.css").read_text(), flags=re.S)
WIDE = "@media (min-width: 1000px) {"


def _block(head: str) -> str:
    at = CSS.index(head) + len(head)
    return CSS[at : CSS.index("\n}", at)]


def test_a_narrow_screen_slides_the_section_in_over_the_list() -> None:
    body = re.search(r"\n\.stx__body \{([^}]*)\}", CSS).group(1)  # type: ignore[union-attr]
    assert "visibility: hidden" in body and "translateX(36px)" in body
    shown = re.search(r"\.stx\.is-in > \.stx__body \{([^}]*)\}", CSS).group(1)  # type: ignore[union-attr]
    assert "visibility: visible" in shown and "transform: none" in shown


def test_a_wide_screen_keeps_the_list_beside_the_section_at_the_width_the_script_reads() -> None:
    wide = _block(WIDE)
    assert re.search(r"\.stx__nav \{[^}]*width: 300px", wide)
    assert re.search(
        r"\.stx__body, \.stx\.is-in > \.stx__body \{[^}]*left: 300px[^}]*visibility: visible", wide
    )
    assert "'(min-width: 1000px)'" in (ROOT / "js/settings/sections.js").read_text()
