"""Phones: fields stay at 16px on every touch screen, the player keeps its mute and its 44px
buttons, All titles keeps one column count across the common phone widths, and a disabled field
reads disabled.

These read the stylesheet rules, since no layout engine runs here.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS_DIR = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "static" / "css"


def _blocks(sheet: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", (CSS_DIR / sheet).read_text(encoding="utf-8"), flags=re.S)
    return re.findall(r"@media ([^{]*)\{((?:[^{}]*\{[^{}]*\})*)\s*\}", css)


def _plain(sheet: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", (CSS_DIR / sheet).read_text(encoding="utf-8"), flags=re.S)
    return re.sub(r"@media [^{]*\{(?:[^{}]*\{[^{}]*\})*\s*\}", "", css)


def test_fields_are_16px_on_touch_screens_whatever_their_width() -> None:
    for sheet, selector in (
        ("_forms.css", ".inp"),
        ("_login.css", ".login-field input"),
        ("_password.css", ".pwd--auth .pwd-box > .inp"),
    ):
        touch = [b for head, b in _blocks(sheet) if "(pointer: coarse)" in head]
        assert any(selector in b and "font-size: 16px" in b for b in touch), sheet


def test_player_keeps_the_mute_on_a_wide_touch_screen_and_drops_it_only_where_the_row_is_full() -> None:
    blocks = _blocks("_player.css")
    mute = [head.strip() for head, b in blocks if re.search(r"\.player-volume\s*\{", b)]
    assert "(max-width: 399.98px) and (pointer: coarse)" in mute
    assert "(pointer: coarse)" not in mute
    assert any(
        "(max-width: 419.98px) and (pointer: coarse)" in head and "gap: 0" in b for head, b in blocks
    )
    for head, b in blocks:
        if "min-width: 38px" in b:
            assert "(pointer: fine)" in head, head


def test_all_titles_is_three_across_from_360_wide() -> None:
    heads = [head.strip() for head, b in _blocks("_watch_grid.css") if "repeat(2," in b]
    assert heads == ["(max-width: 359.98px)"]


def test_a_disabled_field_is_dimmed_and_not_allowed() -> None:
    m = re.search(r"\.inp:disabled[^{]*\{([^}]*)\}", _plain("_forms.css"))
    assert m
    assert "var(--t3)" in m.group(1)
    assert "not-allowed" in m.group(1)
