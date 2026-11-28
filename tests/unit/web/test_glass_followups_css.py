"""Glass loose ends: menus over the search sheet, keyboard rings and blue words on glass in one
colour each, fades on the glass itself, and solid fills where glass would show what is under it."""

from __future__ import annotations

import re

import pytest

from tests.unit.web._css import contrast, rules, style, token
from tests.unit.web.test_glass_css import WHITE, _colour, _glass_over, _over

KEEP = "_watch.css"
PASSWORD = "_password.css"
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


def test_a_menu_of_a_preview_over_the_search_sheet_stands_over_the_preview_sheet_or_none() -> None:
    # The menu takes the focus, which can close the sheet: its place must not hang on has-sheet.
    menu = _z(style(".pv-menu--over", PREVIEW)["z-index"])
    assert menu > int(token("--z-nav"))
    assert menu > _z(style(".pv--over", PREVIEW)["z-index"])


def test_on_a_phone_a_menu_over_the_search_sheet_is_solid_never_a_third_glass() -> None:
    for media in PHONES.split(", "):
        menu = style(".pv-menu--over", PREVIEW, media)
        assert menu["background"] == "var(--g-solid)", media
        assert menu["backdrop-filter"] == menu["-webkit-backdrop-filter"] == "none", media


def _on_glass(colour: str, scope: dict[str, str]) -> float:
    """The contrast of *colour*, its custom properties from *scope*, on the regular glass over a
    white page."""
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    return contrast(_over(_colour(colour, scope), glass), glass)


def test_a_profile_field_rings_in_the_glass_s_ring_and_a_wrong_one_in_red() -> None:
    ring = style('.yo-form .inp.inp:not([aria-invalid="true"]):focus-visible', PASSWORD)
    assert ring["outline-color"] == "var(--g-focus)"
    assert _on_glass(ring["outline-color"], {}) >= 3
    for field in (".pwd", ".yo-form"):
        wrong = style(f'{field} .inp.inp[aria-invalid="true"]:focus-visible', PASSWORD)
        assert wrong["outline-color"] == "var(--danger-text)", field
    # The Profile pane is glass, whose red is its own.
    assert _on_glass("var(--danger-text)", {"--danger-text": "var(--g-danger-text)"}) >= 3


@pytest.mark.parametrize(
    ("sheet", "head"),
    [
        ("_watch_grid.css", (".watch-all__back", ".watch-all__tools")),
        ("_watch_dlgrid.css", (".dlg__back", ".dlg__title", ".dlg__names")),
    ],
)
def test_a_grid_s_stuck_head_rings_only_in_the_glass_s_ring(
    sheet: str, head: tuple[str, ...]
) -> None:
    seen = 0
    for _, sels, decls in rules(sheet):
        if not any(":focus-visible" in sel and any(h in sel for h in head) for sel in sels):
            continue
        for prop in ("outline", "outline-color"):
            if prop in decls and decls[prop] != "none":
                seen += 1
                assert "var(--g-focus)" in decls[prop], (sels, decls[prop])
    assert seen >= len(head)


def test_a_rows_menu_ticks_in_a_blue_that_keeps_4_5_to_1_on_its_glass() -> None:
    tick = style(".rec-menu .watch-keep-menu__item > .ico", "_watch_browse.css")["color"]
    assert _on_glass(tick, style(".watch-keep-menu", KEEP)) >= 4.5


def test_the_title_s_hero_draws_no_fill_past_its_fading_foot() -> None:
    # A fill on the hero itself is not masked: it ends on a line lighter than the glass below.
    for _, sels, decls in rules("_watch_detail.css"):
        for sel in sels:
            if re.search(r"\.ttl__hero(?::[\w-]+\(.*\))*$", sel):
                assert decls.get("background", "none") == "none", sel
    fill = style(".ttl__hero::before", "_watch_detail.css")
    assert fill["background"].startswith("linear-gradient(")
    foot = style(".ttl__hero::after", "_watch_detail.css")["mask-image"]
    assert fill["mask-image"] == fill["-webkit-mask-image"] == foot
    assert foot.startswith("linear-gradient(0deg, transparent,")


def test_the_space_bar_s_parts_grow_without_stretching_free_s_edge() -> None:
    seen = 0
    for _, sels, decls in rules("_watch_space.css"):
        if any(".space-seg" in sel and sel.endswith("::before") for sel in sels):
            seen += 1
            assert "transform" not in decls and "will-change" not in decls, sels
    assert seen >= 4
    # The bar is resized from its middle by --k: the chosen part grows, the others shrink.
    inset = style(".space-seg::before", "_watch_space.css")["inset"]
    assert re.fullmatch(r"calc\(50% - 11px \* var\(--k, 1\)\) 0", inset), inset
    chosen = style(".space-bar.has-active .space-seg.is-active::before", "_watch_space.css")
    others = style(".space-bar.has-active .space-seg::before", "_watch_space.css")
    pressed = style(".space-bar:not(.has-active) .space-seg:active::before", "_watch_space.css")
    assert float(chosen["--k"]) > 1 > float(others["--k"])
    assert float(pressed["--k"]) > 1
