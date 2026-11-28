"""Glass loose ends: menus over the search sheet, keyboard rings and blue words on glass in one
colour each, fades on the glass itself, and solid fills where glass would show what is under it."""

from __future__ import annotations

import re

import pytest

from tests.unit.web._css import CSS_DIR, contrast, px, rgb, rules, style, token
from tests.unit.web.test_glass_css import BLACK, WHITE, _colour, _glass_over, _over

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
    assert seen == len(head)


@pytest.mark.parametrize("page", [WHITE, BLACK])
def test_the_keyboard_s_ring_holds_3_to_1_on_glass_over_any_page_and_on_the_page(
    page: tuple[float, ...],
) -> None:
    ring = _colour("var(--g-focus)", {})
    glass = _glass_over(page, float(token("--g-a")), filtered=True)
    assert contrast(_over(ring, glass), glass) >= 3
    assert contrast(ring[:3], rgb(token("--bg"))) >= 3


# Rings in a colour of their own on purpose: a field marked wrong is red; a white ring where a
# colour already says something under it (a chosen picture, the downloads' progress round the
# profile picture) or on a red button; and a code's next box, whose ring is its border's blue.
OWN_RINGS = {
    ("_password.css", "var(--danger-text)"),
    ("_login.css", "var(--danger-text)"),
    ("_nav.css", "var(--t1)"),
    ("_unsubscribe.css", "var(--t1)"),
    ("_profile_picture.css", "var(--t1)"),
    ("_play_on.css", "var(--accent)"),
}
# How many visible rings each sheet draws in the glass's ring: one dropped to `none` is one lost.
RINGS = {
    "_base.css": 2,
    "_error.css": 1,
    "_history.css": 1,
    "_login.css": 2,
    "_modal.css": 1,
    "_nav.css": 2,
    "_pair.css": 1,
    "_password.css": 2,
    "_play_on.css": 2,
    "_player.css": 17,
    "_profile.css": 1,
    "_settings_pg_panels.css": 1,
    "_tv_remote.css": 3,
    "_ui_feedback.css": 2,
    "_watch.css": 5,
    "_watch_activity.css": 4,
    "_watch_ask.css": 4,
    "_watch_browse.css": 3,
    "_watch_cards.css": 3,
    "_watch_cast.css": 1,
    "_watch_collection.css": 1,
    "_watch_detail.css": 12,
    "_watch_dlgrid.css": 7,
    "_watch_downloading.css": 1,
    "_watch_explore.css": 4,
    "_watch_grid.css": 4,
    "_watch_next.css": 2,
    "_watch_search.css": 7,
    "_watch_space.css": 10,
    "_you.css": 1,
}
FOCUSED = re.compile(r":focus(?:-visible|-within)?\b|\.is-picked\b")


def _rings(sheet: str) -> list[tuple[list[str], str]]:
    """Each visible keyboard ring of *sheet*, forced colours aside: its selectors and its colour.
    A box shadow's ring is a layer of spread alone; the page's colour under it is its gap."""
    found = []
    for where, sels, decls in rules(sheet):
        focused = [s for s in sels if FOCUSED.search(re.sub(r":not\([^)]*\)", "", s))]
        if not focused or any("forced-colors" in w for w in where):
            continue
        for prop in ("outline", "outline-color"):
            colour = re.search(r"var\(--[\w-]+\)", decls.get(prop, "none"))
            if colour:
                found.append((focused, colour[0]))
        for colour in re.findall(r"0 0 0 [1-8]px (var\(--[\w-]+\))", decls.get("box-shadow", "")):
            if colour != "var(--bg)":
                found.append((focused, colour))
    return found


def test_every_keyboard_ring_in_the_app_is_the_glass_s_ring() -> None:
    # --g-focus holds 3:1 on glass over a white page and on the black one; the page's blues do not
    # on glass, and two blues for one ring read as two meanings.
    counted: dict[str, int] = {}
    for path in sorted(CSS_DIR.glob("*.css")):
        for sels, colour in _rings(path.name):
            if (path.name, colour) in OWN_RINGS:
                continue
            assert colour == "var(--g-focus)", (path.name, sels)
            counted[path.name] = counted.get(path.name, 0) + 1
    assert counted == RINGS


def test_in_forced_colours_a_download_card_rings_with_an_outline() -> None:
    # Forced colours drop box shadows, the card's ring everywhere else.
    card = ".dwx:has(> .dw__link:focus-visible, > .dwx__open:focus-visible)"
    ring = style(card, "_watch_dlgrid.css", "(forced-colors: active)")
    assert re.fullmatch(r"\d+px solid Highlight", ring["outline"]), ring
    assert px(ring["outline-offset"]) >= 0


def test_a_download_card_s_line_goes_on_to_a_second_inside_its_foot_and_cuts_no_word() -> None:
    # On a phone "11.7 GB of 11.8 GB · 1 of 1 episode ready" beside its episodes is wider than
    # the line: cut, it read "of 11…".
    foot = style(".dwx__foot", "_watch_dlgrid.css")
    say = style(".dwx__say", "_watch_dlgrid.css")
    assert say.get("white-space", "normal") == "normal"
    assert say.get("text-overflow", "clip") == "clip"
    assert say["-webkit-line-clamp"] == say["line-clamp"] == "2"
    # Balanced, a phrase is not split for one word on the second line.
    assert say["text-wrap"] == "balance"
    assert 2 * float(say["line-height"]) * px(foot["font-size"]) <= px(foot["height"])
    for _, sels, decls in rules("_watch_dlgrid.css"):
        if any(re.search(r"\.dwx__(lead|keep)\b", s) for s in sels):
            assert "text-overflow" not in decls, sels


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


def _eased(curve: str, at: float) -> float:
    """How far a cubic-bezier(...) *curve* has gone at *at* of its time."""
    x1, y1, x2, y2 = (float(n) for n in re.fullmatch(r"cubic-bezier\((.+)\)", curve)[1].split(","))
    lo, hi = 0.0, 1.0
    for _ in range(50):
        s = (lo + hi) / 2
        if 3 * (1 - s) ** 2 * s * x1 + 3 * (1 - s) * s * s * x2 + s**3 < at:
            lo = s
        else:
            hi = s
    return 3 * (1 - s) ** 2 * s * y1 + 3 * (1 - s) * s * s * y2 + s**3


def test_on_a_phone_the_bar_s_islands_stop_blurring_before_a_rising_title_reaches_them() -> None:
    # Sheet, dim and islands blurred at one point would be three layers. The islands end 56px down;
    # on a phone 568px tall the rising sheet reaches them when it has 56px left to go.
    held = style("html.has-ttl :is(.nav-glass[inert], [inert] .nav-glass)", "_nav.css", PHONES)
    factor = float(
        re.fullmatch(
            r"isle-calm calc\(var\(--dur-slow\) \* ([\d.]+)\) step-end forwards", held["animation"]
        )[1]
    )
    rise = style(".ttl.is-entering .ttl__sheet", "_watch_detail.css")["animation"].split()
    assert rise[1] == "var(--dur-slow)"
    curve = token(rise[2][len("var(") : -1])
    reached = next(t / 1000 for t in range(1001) if _eased(curve, t / 1000) >= 1 - 56 / 568)
    assert 0 < factor < reached
