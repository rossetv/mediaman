"""The Settings pane lays out its section list, the section on show and the save bar under them.

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


def _rule(selector: str) -> str:
    return re.search(rf"\n{re.escape(selector)} \{{([^}}]*)\}}", CSS).group(1)  # type: ignore[union-attr]


def test_the_section_list_scrolls_apart_from_the_section_so_it_stays_put_as_the_section_scrolls() -> (
    None
):
    assert "overflow: hidden" in _rule('.you-view[data-view="settings"]')
    assert "position: relative" in _rule(".setg-pg.stx")
    both = _rule(".stx__nav, .stx__body")
    assert "position: absolute; inset: 0;" in both and "overflow-y: auto" in both
    assert "position: sticky" not in CSS


def test_the_save_bar_sits_under_the_sections_scroller_and_keeps_room_for_the_home_indicator() -> (
    None
):
    page = (ROOT.parent / "templates/panes/_settings.html").read_text()
    view = page[page.index('data-view="settings"') :]
    # a sibling after the list and sections, outside the box that scrolls, so it covers no field
    assert view.index('<div class="setg-pg stx"') < view.index('\n  <div class="stx-save"')
    assert view.index('<div class="stx__body">') < view.index('\n  <div class="stx-save"')
    bar = _rule(".stx-save")
    assert "flex: none" in bar
    assert "padding: 12px 16px max(12px, env(safe-area-inset-bottom, 0px));" in bar
