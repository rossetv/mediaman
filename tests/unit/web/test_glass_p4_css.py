"""Glass on a title's sheet, a card's preview and the search sheet: the same material as the bar's,
with what lies on it legible over a white page, its fades on the glass itself, and no glass left
blurring where a surface hands its place to another."""

from __future__ import annotations

import re

import pytest

from tests.unit.web._css import contrast, rules, style, token
from tests.unit.web.test_glass_css import WHITE, _colour, _glass_over, _on, _over, _resolve

DETAIL = "_watch_detail.css"
PREVIEW = "_watch_preview.css"
SEARCH = "_watch_search.css"
WIDE = "(min-width: 700px) and (min-height: 500.02px)"
REDUCED = "(prefers-reduced-motion: reduce)"
# Phones either way up: narrow, or short and touched.
PHONES = "(max-width: 699.98px), (max-height: 500px) and (pointer: coarse)"

# Each glass surface, and the sheet that draws it.
SURFACES = [(".ttl__sheet", DETAIL), (".pv__shell", PREVIEW), (".watch-sheet", SEARCH)]
WORDS = ("--t2", "--t3", "--t4", "--accent-text", "--accent-hi", "--danger-text")


def _sheet_scope() -> dict[str, str]:
    """What the title's sheet resolves its words and fills to; its blue is the page's, lifted."""
    lifted = token("--g-accent").replace("var(--accent)", token("--accent"))
    return {**style(".ttl__sheet", DETAIL), "--accent": lifted}


def _white_glass() -> tuple[float, ...]:
    return _glass_over(WHITE, float(token("--g-a")), filtered=True)


@pytest.mark.parametrize(("surface", "sheet"), [*SURFACES, (".cry-plate", DETAIL)], ids=lambda v: v)
def test_each_surface_is_glass_with_the_same_filter_in_every_engine(
    surface: str, sheet: str
) -> None:
    drawn = style(surface, sheet)
    assert drawn["background"] == "var(--g-regular)"
    # Safari reads only the prefixed one; a different value would be another material there.
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "var(--g-filter)"


@pytest.mark.parametrize(("surface", "sheet"), [(".ttl__sheet", DETAIL), (".watch-sheet", SEARCH)])
def test_a_surface_with_its_own_edge_catches_the_light_along_its_top(
    surface: str, sheet: str
) -> None:
    drawn = style(surface, sheet)
    assert drawn["border-top-color"] == "var(--g-lit)"
    assert _colour("var(--g-lit)", {})[3] > _colour("var(--g-edge)", {})[3]


@pytest.mark.parametrize(("surface", "sheet"), SURFACES)
def test_every_word_and_ring_on_a_surface_takes_a_colour_lifted_for_its_glass(
    surface: str, sheet: str
) -> None:
    declared = style(surface, sheet)
    glass = _white_glass()
    for name in WORDS:
        assert name in declared, f"{surface} leaves {name} at its colour for solid grey"
        assert contrast(_on(_resolve(declared[name]), glass), glass) >= 4.5, (surface, name)
    # A fill on it is a veil of the glass, never the solid grey that would hide it.
    for step in ("--s1", "--s2", "--s3", "--s4", "--s5"):
        assert declared[step] == f"var(--g{step[1:]})", (surface, step)
    ring = _colour(declared["--accent"], {"--accent": token("--accent")})
    assert contrast(_over(ring, glass), glass) >= 3, surface


def test_the_page_under_a_title_is_a_plain_dim_with_the_title_s_light_blur() -> None:
    dim = style(".ttl__dim", DETAIL)
    assert dim["background"] == "var(--g-dim)"
    assert dim["backdrop-filter"] == dim["-webkit-backdrop-filter"] == "var(--g-dim-blur)"


def test_the_heading_lies_on_the_picture_s_scrim_with_a_shadow_under_its_words() -> None:
    assert style(".ttl__hero::after", DETAIL)["background"] == "var(--g-hero-scrim)"
    assert style(".ttl__head", DETAIL)["text-shadow"] == "var(--g-hero-text-shadow)"


def test_the_shade_under_the_heading_is_dark_where_the_sheet_s_own_grey_is_glass() -> None:
    # --s1 is see-through on the glass, so a shade drawn in it would leave the words on the picture.
    shades = [
        decls["background"]
        for _, sels, decls in rules(DETAIL)
        for sel in sels
        if sel in (".ttl__head::before", ".cry-shade > i > i") and "background" in decls
    ]
    assert len(shades) >= 3
    for shade in shades:
        assert "--s1" not in shade and "var(--ttl-shade)" in shade, shade
    darkest = _colour(style(".ttl", DETAIL)["--ttl-shade"], {})
    assert max(darkest[:3]) == 0 and darkest[3] >= 0.5


def test_the_hero_s_picture_and_its_shades_fade_out_at_its_foot_into_the_glass_below() -> None:
    # Dark to its last row, the hero would meet the body's glass at a hard edge over a light page.
    for piece in (
        ".ttl__hero::after",
        ".ttl__hero > :is(.ttl__hero-skel, .ttl__art)",
        ".ttl__fill",
    ):
        mask = style(piece, DETAIL)
        assert mask["mask-image"] == mask["-webkit-mask-image"], piece
        assert re.match(r"linear-gradient\(0deg, transparent[ ,]", mask["mask-image"]), piece
    for _, sels, decls in rules(DETAIL):
        if {".ttl__head::before", ".cry-shade > i > i"} & set(sels) and "background" in decls:
            assert decls["background"].startswith("linear-gradient(0deg, transparent, "), sels


def test_close_on_a_title_keeps_its_x_at_rest_lit_and_pressed_over_a_white_picture() -> None:
    x = _colour(style(".ttl__close", DETAIL)["color"], _sheet_scope())
    for state in (".ttl__close", ".ttl__close:is(:hover, :active)"):
        under = _over(_colour(style(state, DETAIL)["background"], _sheet_scope()), WHITE)
        assert contrast(_over(x, under), under) >= 4.5, state


def test_the_stand_ins_of_a_preview_turning_into_its_title_look_as_what_they_stand_for() -> None:
    # A stand-in drawn otherwise jumps to the real thing when the move ends, or from it as it starts.
    assert (
        style(".cry-shade > i::before", DETAIL)["background"]
        == style(".ttl__hero::after", DETAIL)["background"]
    )
    ring = style(".cry-plate::before", DETAIL)["box-shadow"]
    assert ring == style(".pv__shadow", PREVIEW)["box-shadow"]
    landed = style(".cry-plate::after", DETAIL)
    sheet = style(".ttl__sheet", DETAIL)
    assert [landed[k] for k in ("border-top-color", "box-shadow")] == [
        sheet[k] for k in ("border-top-color", "box-shadow")
    ]


# A see-through coloured chip on the sheet, and the classes its look comes from.
CHIPS = [
    (".ttl-rating", (".ttl-rating", ".ttl-rating--tmdb")),
    (".ttl-rating", (".ttl-rating", ".ttl-rating--imdb")),
    (".ttl-rating", (".ttl-rating", ".ttl-rating--meta")),
    (".ttl-rating", (".ttl-rating", ".ttl-rating--rt")),
    (".watch-btn--keep", (".watch-btn--keep",)),
    (".dl-state:not(.dl-state--none)", (".dl-state--queued",)),
    (".dl-state:not(.dl-state--none)", (".dl-state--downloading",)),
    (".dl-state:not(.dl-state--none)", (".dl-state--done",)),
]


@pytest.mark.parametrize(
    ("floored", "looks"), CHIPS, ids=lambda v: v[-1] if isinstance(v, tuple) else v
)
def test_a_tinted_chip_on_the_sheet_lies_on_a_dark_floor_and_its_words_hold_over_a_white_page(
    floored: str, looks: tuple[str, ...]
) -> None:
    assert style(floored, DETAIL)["box-shadow"] == "var(--g-chip-floor)"
    scope = _sheet_scope()
    drawn = {k: v for look in looks for k, v in style(look, DETAIL).items()}
    floor = _colour(re.search(r"rgb\(.+\)", token("--g-chip-floor"))[0], {})
    under = _over(_colour(drawn["background"], scope), _over(floor, _white_glass()))
    words = _over(_colour(drawn["color"], scope), under)
    assert contrast(words, under) >= 4.5, (looks, round(contrast(words, under), 2))


def test_the_other_chips_on_the_sheet_take_the_floor_too() -> None:
    assert style(".ttl__cert", DETAIL)["box-shadow"] == "var(--g-chip-floor)"
    assert style(".ttl .watch-ep__state", DETAIL)["box-shadow"] == "var(--g-chip-floor)"


@pytest.mark.parametrize(
    ("piece", "media"),
    [(".ttl-facts__menu", None), (".ttl__bar::before", None), (".ttl__sheet::before", WIDE)],
)
def test_what_lies_on_the_sheet_s_glass_is_its_tint_solid_never_a_second_glass(
    piece: str, media: str | None
) -> None:
    drawn = style(piece, DETAIL, media)
    assert drawn["background"] == "var(--g-solid)"
    assert "backdrop-filter" not in drawn


def test_a_lit_item_of_the_title_s_keep_menu_is_lighter_than_the_menu_and_its_words_hold() -> None:
    scope = _sheet_scope()
    menu = _over(_colour(style(".ttl-facts__menu", DETAIL)["background"], scope), _white_glass())
    lit = style(
        ".ttl-facts__menu .watch-keep-menu__item:is(:hover, :focus-visible, :active)", DETAIL
    )
    under = _over(_colour(lit["background"], scope), menu)
    assert sum(under) > sum(menu) and contrast(under, menu) >= 1.1
    for word in ("var(--t1)", "var(--t2)", "var(--danger-text)"):
        assert contrast(_over(_colour(word, scope), under), under) >= 4.5, word


@pytest.mark.parametrize(
    ("selector", "sheet", "media"),
    [
        (".ttl.is-carried .ttl__sheet", DETAIL, None),
        (".ttl.is-film .ttl__sheet", DETAIL, None),
        (".pv:is(.is-carried, .is-held) > .pv__shell", PREVIEW, None),
        (".pv.is-handed > .pv__shell", PREVIEW, None),
    ],
)
def test_a_surface_that_hands_its_place_to_another_takes_its_blur_away_with_its_tint(
    selector: str, sheet: str, media: str | None
) -> None:
    # A blur left on with no tint over it would blur the picture or the page that comes in its place.
    drawn = style(selector, sheet, media)
    assert drawn["background"] == "none"
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "none"


def test_a_title_opened_from_its_preview_is_glass_from_under_its_hero_down() -> None:
    glass = style(".ttl.is-film .ttl__sheet::after", DETAIL)
    # From where the hero's foot starts to fade into it.
    assert glass["inset"].startswith("calc(var(--ttl-hero) - var(--ttl-foot))")
    assert glass["background"] == "var(--g-regular)"
    assert glass["backdrop-filter"] == glass["-webkit-backdrop-filter"] == "var(--g-filter)"


PARTS = ":is(.pv__shadow, .pv__shell, .pv__lift)"


def test_a_preview_that_fades_away_fades_its_glass_and_not_the_parent_that_holds_it() -> None:
    # Glass under a parent with opacity below 1 sees none of the page while it fades.
    assert "opacity" not in style(".pv.is-fading", PREVIEW)
    assert style(f".pv.is-fading > {PARTS}", PREVIEW)["opacity"] == "0"


def test_under_reduced_motion_a_preview_fades_in_by_its_glass_and_not_its_parent() -> None:
    assert "opacity" not in style(".pv", PREVIEW, REDUCED)
    assert style(f".pv > {PARTS}", PREVIEW, REDUCED)["opacity"] == "0"
    assert style(f".pv.is-open:not(.is-fading) > {PARTS}", PREVIEW, REDUCED)["opacity"] == "1"


# A red item of a keep menu: the menu, what the item is drawn with in each state.
DANGER = [
    ("rest", ()),
    ("hover", ((".watch-keep-menu__item:hover", "_watch.css"),)),
    ("pressed", ((".watch-keep-menu__item:active", "_watch.css"),)),
    (
        "downloads hover",
        (
            (".watch-keep-menu__item:hover", "_watch.css"),
            (
                ".dwx-menu .watch-keep-menu__item.is-danger:is(:hover, :focus-visible)",
                "_watch_dlgrid.css",
            ),
        ),
    ),
]


@pytest.mark.parametrize(("state", "lit"), DANGER, ids=[d[0] for d in DANGER])
def test_a_red_item_of_a_keep_menu_holds_4_5_to_1_over_a_white_page_at_rest_lit_and_pressed(
    state: str, lit: tuple[tuple[str, str], ...]
) -> None:
    menu = style(".watch-keep-menu", "_watch.css")
    fill = menu["background"]
    # Glass shows the page under it; a solid grey hides it.
    under = _white_glass() if "--g-" in fill else _over(_colour(fill, menu), (0.0, 0.0, 0.0))
    # The last rule that lights it wins: its fill takes the place of the ones before it.
    fills = [style(selector, sheet).get("background") for selector, sheet in lit]
    fills = [f for f in fills if f]
    if fills:
        under = _over(_colour(fills[-1], menu), under)
    # The red's rules come after the menu's own, so a lit item keeps its red.
    red = _colour(style(".watch-keep-menu__item.is-danger", "_watch_grid.css")["color"], menu)
    dwx = _colour(
        style(".dwx-menu .watch-keep-menu__item.is-danger", "_watch_dlgrid.css")["color"], menu
    )
    for word in (red, dwx):
        assert contrast(_over(word, under), under) >= 4.5, (
            state,
            round(contrast(_over(word, under), under), 2),
        )


def test_on_a_phone_a_menu_of_a_preview_over_the_search_sheet_is_solid_not_a_third_glass() -> None:
    # The search sheet and the preview over it are two layers of glass already.
    menu = style(".pv--over .pv-menu", PREVIEW, PHONES)
    assert menu["background"] == "var(--g-solid)"
    assert menu["backdrop-filter"] == menu["-webkit-backdrop-filter"] == "none"


def test_on_a_phone_close_and_the_trailer_s_frame_take_no_blur_over_the_sheet_and_its_dim() -> None:
    # The dim and the sheet blur already; a blur of their own would be a third layer.
    for piece in (".ttl__close", ".ttl__player"):
        drawn = style(piece, DETAIL, PHONES)
        assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "none", piece
    assert style(".ttl__player", DETAIL, PHONES)["background"] == "var(--pl)"


def test_close_on_a_title_rings_in_the_ring_made_for_glass() -> None:
    assert style(".ttl__close:focus-visible", DETAIL)["outline"] == "2px solid var(--g-focus)"


def test_opening_search_fades_the_name_s_logo_and_its_glass_never_the_name_itself() -> None:
    # Under a fading parent, the island's glass stops blurring and flashes light.
    faded = {
        sel
        for _, sels, decls in rules(SEARCH)
        if decls.get("opacity") == "0" or "opacity" in decls.get("transition", "")
        for sel in sels
    }
    assert not {sel for sel in faded if re.search(r"\.brand\)?$", sel) or ":is(.brand" in sel}
    shown = ".wbar:has(> .watch-search.is-focus) > .brand"
    assert {f"{shown} > *", f"{shown}::before"} <= faded
