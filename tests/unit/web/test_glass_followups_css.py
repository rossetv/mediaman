"""Glass loose ends: menus over the search sheet, keyboard rings and blue words on glass in one
colour each, fades on the glass itself, and solid fills where glass would show what is under it."""

from __future__ import annotations

import re

from tests.unit.web._css import style, token

KEEP = "_watch.css"
PREVIEW = "_watch_preview.css"
# Phones either way up: narrow, or short and touched.
PHONES = "(max-width: 699.98px), (max-height: 500px) and (pointer: coarse)"


def _z(value: str) -> int:
    """A z-index of a whole number or calc(var(--z-*) + n)."""
    found = re.fullmatch(r"calc\(var\((--z-[\w-]+)\) ([+-]) (\d+)\)", value)
    if not found:
        return int(value)
    step = int(found[3])
    return int(token(found[1])) + (step if found[2] == "+" else -step)


def test_a_menu_opened_over_the_search_sheet_stands_over_the_bar_and_the_preview() -> None:
    menu = _z(style("html.has-sheet .watch-keep-menu", KEEP)["z-index"])
    assert menu > int(token("--z-nav"))
    assert menu > _z(style(".pv--over", PREVIEW)["z-index"])


def test_on_a_phone_a_menu_over_the_search_sheet_is_solid_never_a_third_glass() -> None:
    for media in PHONES.split(", "):
        menu = style("html.has-sheet .watch-keep-menu", KEEP, media)
        assert menu["background"] == "var(--g-solid)", media
        assert menu["backdrop-filter"] == menu["-webkit-backdrop-filter"] == "none", media
