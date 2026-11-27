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
    nav = re.search(r"\.stx__nav \{[^}]*width: (\d+)px", wide).group(1)  # type: ignore[union-attr]
    # the list keeps to about a third of the wide panel, and the section starts where it ends
    assert 320 <= int(nav) <= 360
    assert re.search(
        r"\.stx__body, \.stx\.is-in > \.stx__body \{[^}]*left: "
        + nav
        + r"px[^}]*visibility: visible",
        wide,
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
    assert re.search(r"padding: 12px \d+px max\(12px, env\(safe-area-inset-bottom, 0px\)\);", bar)


def test_the_save_bar_starts_its_line_where_the_section_list_does() -> None:
    wide = re.search(r"padding: 12px (\d+)px", _rule(".stx-save")).group(1)  # type: ignore[union-attr]
    nav = re.search(r"\.stx__nav \{[^}]*padding: \S+ \S+ \S+ (\d+)px", _block(WIDE)).group(1)  # type: ignore[union-attr]
    assert wide == nav
    # on a phone both come in to 16px, the bar's rule after its own so it wins
    at = CSS.index("\n.stx-save {")
    phone = re.search(
        r"@media \(max-width: 699\.98px\) \{\s*\.stx-save \{ padding-inline: 16px; \}", CSS
    )
    assert phone and phone.start() > at
    assert re.search(r"\.stx__nav, \.stx__body \{ padding-right: 16px; padding-left: 16px; \}", CSS)


def test_a_focused_section_link_draws_its_ring_inside_the_list_that_clips_it() -> None:
    assert "overflow: hidden" in _rule(".stx-list")
    assert "outline-offset: -2px" in _rule(".stx-link:focus-visible")


def test_help_lines_stop_at_a_reading_measure_however_wide_the_panel() -> None:
    for sel in (".setg-pg .setg-row-sub", ".setg-pg .setg-card-hd .sub"):
        width = re.search(r"max-width: (\d+)ch", _rule(sel))
        assert width and 50 <= int(width.group(1)) <= 66, sel


def test_beside_the_list_a_card_s_lists_and_times_line_up_in_one_column() -> None:
    sel = '.setg-pg .setg-row select.inp, .setg-pg .setg-row input[type="time"].inp'
    base = CSS.index("\n" + sel + " {")
    wide = re.search(
        r"@media \(min-width: 1000px\) \{\s*" + re.escape(sel) + r" \{ width: (\d+)px; \}", CSS
    )
    # after the rule that lets each be as wide as it holds, so it wins at that width
    assert wide and wide.start() > base
    assert f"max-width: {wide.group(1)}px" in _rule(sel)
