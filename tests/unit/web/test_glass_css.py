"""Glass: one material named once in the tokens, drawn by the top bar's islands, the profile menu,
toasts, the side panels (the pane over Watch, Space and the keep menu), the sign-in card and the pages that
stand in it, and a download page's bar and episodes, legible over a white page, and solid wherever glass
cannot or should not be drawn."""

from __future__ import annotations

import colorsys
import math
import re

import pytest

from tests.unit.web._css import (
    CSS_DIR,
    _top_level,
    contrast,
    medias,
    px,
    rgb,
    rules,
    style,
    text,
    token,
)

NAV = "_nav.css"
TOASTS = "_ui_feedback.css"
YOU = "_you.css"
PROFILE = "_profile.css"
HISTORY = "_history.css"
SETTINGS = "_settings_pg.css"
PANELS = "_settings_pg_panels.css"
SPACE = "_watch_space.css"
KEEP = "_watch.css"
PHONE = "(max-width: 699.98px)"
LOGIN = "_login.css"
SHARE = "_share.css"
DL_PAGE = "_download_page.css"
GRID = "_watch_grid.css"
DL_GRID = "_watch_dlgrid.css"
EPISODES = ".share-eps .dl-hero-episodes"
NO_BLUR = "@supports not"
WHITE = (255.0, 255.0, 255.0)
BLACK = (0.0, 0.0, 0.0)

# Each surface, the sheet that draws it, and the custom properties it draws its glass with.
SURFACES = [
    (".profile-panel", NAV, "background"),
    (".ui-toast", TOASTS, "background-color"),
    (".you__panel::before", YOU, "background"),
    (".space__panel", SPACE, "background"),
    (".watch-keep-menu", KEEP, "background"),
    (".hx-menu", HISTORY, "background"),
    (".login-card", LOGIN, "background"),
    (".share-bar .btn", SHARE, "background"),
    (EPISODES, DL_PAGE, "background"),
]
# What sits on the glass of each surface: what its words, fills and blue and red words resolve to.
ON_GLASS = [
    (".nav-end", NAV),
    (".nav-back", NAV),
    (".nav-glass .watch-search", NAV),
    (".profile-panel", NAV),
    (".ui-toast", TOASTS),
    (".you__panel", YOU),
    (".space__panel", SPACE),
    (".watch-keep-menu", KEEP),
    (".login-card", LOGIN),
    (EPISODES, DL_PAGE),
]
WORDS = ("--t2", "--t3", "--t4", "--accent-text", "--danger-text")


def _root(where: str = "") -> dict[str, str]:
    """What :root declares in the tokens sheet, inside the at-rule holding *where* (outside all
    at-rules when empty), later winning."""
    merged: dict[str, str] = {}
    for at, selectors, decls in rules("_tokens.css"):
        if ":root" in selectors and (any(where in a for a in at) if where else not at):
            merged.update(decls)
    return merged


def _glass_over(page: tuple[float, ...], tint_alpha: float, filtered: bool) -> tuple[float, ...]:
    """The colour of the glass over a flat *page*: the filter's brightness, then the tint."""
    bright = 1.0
    if filtered:
        bright = float(re.search(r"brightness\(([\d.]+)\)", token("--g-filter"))[1])
    tint = rgb(token("--g-tint"))
    return tuple(
        tint_alpha * t + (1 - tint_alpha) * p * bright for t, p in zip(tint, page, strict=True)
    )


def _on(colour: str, under: tuple[float, ...]) -> tuple[float, ...]:
    *channels, alpha = (*rgb(colour), 1.0)[:4]
    return tuple(alpha * c + (1 - alpha) * u for c, u in zip(channels, under, strict=True))


def _resolve(value: str) -> str:
    found = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
    return token(found[1]) if found else value


def _colour(value: str, scope: dict[str, str]) -> tuple[float, float, float, float]:
    """*value* as straight RGBA, its custom properties taken from *scope* before the tokens, and
    color-mix() mixed as a browser does in srgb (premultiplied)."""
    value = re.sub(
        r"var\((--[\w-]+)\)", lambda m: scope.get(m[1]) or token(m[1]), value.strip(), count=1
    )
    if "var(" in value:
        return _colour(value, scope)
    if value == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    if value.startswith("color-mix("):
        _, *parts = _top_level(value[len("color-mix(") : -1], ",")
        mixed = []
        for part in parts:
            found = re.fullmatch(r"(.+?)(?:\s+([\d.]+)%)?", part.strip())
            mixed.append((_colour(found[1], scope), found[2] and float(found[2]) / 100))
        (a, pa), (b, pb) = mixed
        pa = 1 - pb if pa is None and pb is not None else 0.5 if pa is None else pa
        pb = 1 - pa if pb is None else pb
        alpha = pa * a[3] + pb * b[3]
        channels = (
            (pa * a[3] * x + pb * b[3] * y) / alpha if alpha else 0.0
            for x, y in zip(a[:3], b[:3], strict=True)
        )
        return (*channels, alpha)
    r, g, b, *alpha = rgb(value)
    return (r, g, b, alpha[0] if alpha else 1.0)


def _over(colour: tuple[float, ...], under: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(
        colour[3] * c + (1 - colour[3]) * u for c, u in zip(colour[:3], under, strict=True)
    )


def test_the_bar_s_islands_are_glass_of_the_thin_weight() -> None:
    bar = style(".nav-glass", NAV)
    assert bar["--isle"] == "var(--g-thin)"
    assert bar["--isle-blur"] == "var(--g-filter)"
    assert bar["--isle-edge"] == "var(--g-edge)" and bar["--isle-lit"] == "var(--g-lit)"
    for island in (".nav-back", ".nav-end"):
        drawn = style(island, NAV)
        assert drawn["background"] == "var(--isle)"
        assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "var(--isle-blur)"


@pytest.mark.parametrize(
    ("island", "sheet"),
    [
        (".nav-back", NAV),
        (".nav-end", NAV),
        (".nav-glass .brand::before", NAV),
        (".wbar .watch-search::before", "_watch_search.css"),
    ],
)
def test_each_island_catches_the_light_along_its_top_edge(island: str, sheet: str) -> None:
    scope = style(".nav-glass", NAV)
    drawn = style(island, sheet)
    top = _colour(drawn["border-top-color"], scope)
    edge = _colour(drawn["border"].split(" ", 2)[2], scope)
    assert top[3] > edge[3], island


def test_the_search_field_s_glass_takes_the_same_light_as_the_other_islands() -> None:
    lit = style(".nav-glass.wbar .watch-search::before", NAV)["box-shadow"]
    assert lit == style(".nav-end", NAV)["box-shadow"] == style(".profile-panel", NAV)["box-shadow"]


def _glass_parents() -> set[str]:
    """The last class of each element whose ::before is the glass it is drawn on."""
    return {
        re.findall(r"\.[\w-]+", sel.removesuffix("::before"))[-1]
        for sheet in (NAV, "_watch_search.css")
        for _, sels, decls in rules(sheet)
        if "backdrop-filter" in decls
        for sel in sels
        if sel.endswith("::before")
    }


def test_signing_in_and_out_never_fades_an_element_whose_glass_is_its_before() -> None:
    # Glass under a fading parent sees none of the page while it fades: the fade goes on the
    # glass and on what lies on it, and the parent only moves.
    css = re.sub(r"/\*.*?\*/", "", (CSS_DIR / NAV).read_text(encoding="utf-8"), flags=re.S)
    frames = dict(re.findall(r"@keyframes ([\w-]+) \{(.*?)\}\n", css))
    parents = _glass_parents()
    assert ".watch-search" in parents
    checked = 0
    for _, sels, decls in rules(NAV):
        for sel in sels:
            last = re.findall(r"\.[\w-]+|::?[\w-]+|>\s*\*", sel)
            if (
                not sel.startswith(("html.is-arriving", "html.is-leaving"))
                or "animation" not in decls
            ):
                continue
            if last and last[-1] in parents:
                checked += 1
                for name in re.findall(r"\bmm-[\w-]+", decls["animation"]):
                    assert "opacity" not in frames[name], (sel, name)
    assert checked


@pytest.mark.parametrize(("selector", "sheet", "fill"), SURFACES)
def test_a_menu_and_a_toast_are_glass_with_the_same_filter_in_every_engine(
    selector: str, sheet: str, fill: str
) -> None:
    drawn = style(selector, sheet)
    assert drawn[fill] in ("var(--g-thin)", "var(--g-regular)")
    # Safari reads only the prefixed one; a different value would be another material there.
    assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "var(--g-filter)"
    assert drawn["border-top-color"] == "var(--g-lit)"


def test_the_glass_weights_share_one_tint_and_no_text_weight_is_thinner_than_52() -> None:
    for weight in ("--g-thin", "--g-regular"):
        assert re.fullmatch(r"rgb\(var\(--g-tint\) / var\(--g-a\)\)", token(weight)), weight
    assert float(token("--g-a")) >= 0.52


@pytest.mark.parametrize(
    "words", ["#fff", "--g-t2", "--g-t3", "--g-accent-text", "--g-danger-text"]
)
def test_words_on_glass_keep_4_5_to_1_over_a_white_page(words: str) -> None:
    colour = words if words.startswith("#") else token(words)
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    assert contrast(_on(colour, glass), glass) >= 4.5
    # Where nothing halves the page behind, the tint alone holds them.
    solid = _glass_over(WHITE, float(_root(NO_BLUR)["--g-a"]), filtered=False)
    assert contrast(_on(colour, solid), solid) >= 4.5


@pytest.mark.parametrize(("selector", "sheet"), ON_GLASS)
def test_every_word_on_a_glass_surface_takes_a_colour_lifted_for_glass(
    selector: str, sheet: str
) -> None:
    declared = style(selector, sheet)
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    for name in WORDS:
        assert name in declared, f"{selector} leaves {name} at its colour for solid grey"
        assert contrast(_on(_resolve(declared[name]), glass), glass) >= 4.5, (selector, name)
    # A fill inside glass is a veil of it, never the solid grey that would hide it.
    assert declared["--s3"] == "var(--g-s3)"


# Every control with words on glass: the sheet that draws it, the classes it takes its look from,
# the rule that sets what its glass resolves to, and whether its state's fill takes the place of
# its glass's tint (its own island) or lies over it.
CONTROLS = [
    (".ui-toast__action", TOASTS, (".ui-toast__action",), ".ui-toast", False),
    (".nav-back", NAV, (".nav-back",), ".nav-back", True),
    (".nav-noscript", NAV, (".nav-noscript",), ".nav-noscript", True),
    (".profile-btn", NAV, (".profile-btn",), ".nav-end", False),
    (".profile-item", NAV, (".profile-item",), ".profile-panel", False),
    (
        ".profile-item--danger",
        NAV,
        (".profile-item", ".profile-item--danger"),
        ".profile-panel",
        False,
    ),
    (".profile-close", NAV, (".profile-close",), ".profile-panel", False),
    (".nav-glass .brand", NAV, (".brand", ".nav-glass .brand"), ".nav-glass", False),
]
STATE = r"(?::hover|:active|:focus-visible|\[aria-current\]|\[aria-expanded=\"true\"\])"


def _weight(selector: str) -> int:
    return len(re.findall(r"(?<!:)[.:\[]", selector))


def _states(control: str, sheet: str) -> list[str]:
    """The state suffixes *control* is drawn in, rest ("") first."""
    found = {
        sel[len(control) :]
        for _, sels, _ in rules(sheet)
        for sel in sels
        if re.fullmatch(re.escape(control) + STATE + "+", sel)
    }
    return ["", *sorted(found)]


def _in_state(bases: tuple[str, ...], state: str, sheet: str, part: str = "") -> dict[str, str]:
    """What the rules for *bases* and *bases* in each piece of *state* declare for the element (or
    its *part*, as "::before"), in every condition but the ones that turn glass solid."""
    pieces = re.findall(STATE, state)
    wanted = {b + p + part for b in bases for p in ["", *pieces]}
    hits = [
        (_weight(sel), i, decls)
        for i, (where, sels, decls) in enumerate(rules(sheet))
        if not any("forced-colors" in w or "prefers-" in w for w in where)
        for sel in sels
        if sel in wanted
    ]
    merged: dict[str, str] = {}
    for _, _, decls in sorted(hits, key=lambda h: h[:2]):
        merged.update(decls)
    return merged


@pytest.mark.parametrize(("control", "sheet", "bases", "surface", "own_island"), CONTROLS)
def test_a_control_s_words_on_glass_keep_4_5_to_1_over_a_white_page_in_every_state(
    control: str, sheet: str, bases: tuple[str, ...], surface: str, own_island: bool
) -> None:
    scope = {**style(".nav-glass", NAV), **style(surface, sheet)}
    filtered = _glass_over(WHITE, 0.0, filtered=True)
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    states = _states(control, sheet)
    assert len(states) > 1, f"{control} has no state to check"
    for state in states:
        drawn = _in_state(bases, state, sheet)
        fill = drawn.get("background-color") or drawn.get("background")
        lit = _in_state(bases, state, sheet, "::before")
        if lit.get("opacity") == "1":
            fill = style(bases[-1] + "::before", sheet)["background"]
        under = glass
        if fill and fill != "var(--isle)":
            under = _over(_colour(fill, scope), filtered if own_island else glass)
        words = _over(_colour(drawn.get("color", "var(--t1)"), scope), under)
        assert contrast(words, under) >= 4.5, (control + state, round(contrast(words, under), 2))


def test_the_name_s_blue_on_its_island_keeps_4_5_to_1_and_stays_the_brand_s_blue() -> None:
    blue = _colour(style(".nav-glass .brand .wm-man", NAV)["stroke"], {})
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    assert contrast(_over(blue, glass), glass) >= 4.5
    hue = colorsys.rgb_to_hsv(*(c / 255 for c in blue[:3]))[0]
    brand = colorsys.rgb_to_hsv(*(c / 255 for c in rgb(token("--accent"))))[0]
    assert abs(hue - brand) * 360 < 10


def test_the_name_is_on_its_island_s_glass_before_the_page_moves_under_it() -> None:
    # A white page or a pale poster under the bar at the top would take the name's white.
    for where, sels, decls in rules(NAV):
        if ".nav-glass .brand::before" in sels and not any("forced-colors" in w for w in where):
            assert decls.get("opacity", "1") != "0", where
    assert style(".nav-glass .brand::before", NAV)["background"] == "var(--isle)"
    # At rest only its edges and shadow wait for the page to move.
    rest = style(".nav-glass:not(.is-scrolled) .brand::before", NAV)
    assert set(rest) == {"border-color", "box-shadow"}, rest


@pytest.mark.parametrize("variant", [".ui-toast--success", ".ui-toast--error", ".ui-toast--info"])
def test_a_toast_s_colour_is_a_wash_over_its_glass_and_never_covers_the_tint(variant: str) -> None:
    drawn = style(variant, TOASTS)
    assert "background" not in drawn and "background-color" not in drawn, drawn
    # The wash fades out into the glass, so the tint holds the words after its start.
    assert (
        _colour(
            _top_level(drawn["background-image"][len("linear-gradient(") : -1], ",")[-1].split()[0],
            {},
        )[3]
        == 0
    )


@pytest.mark.parametrize(
    "where", [NO_BLUR, "(prefers-reduced-transparency: reduce)", "(prefers-contrast: more)"]
)
def test_where_glass_cannot_or_should_not_be_drawn_it_is_solid(where: str) -> None:
    solid = _root(where)
    assert solid["--g-filter"] == "none"
    for alpha in ("--g-a", "--g-a-thick", "--g-a-player", "--g-a-player-hover"):
        assert float(solid[alpha]) >= 0.94, (where, alpha)


def test_in_forced_colours_glass_is_a_system_coloured_box() -> None:
    forced = _root("(forced-colors: active)")
    assert forced["--g-filter"] == "none"
    for weight in ("--g-thin", "--g-regular", "--g-thick", "--g-player"):
        assert forced[weight] == "Canvas", weight
    assert forced["--g-edge"] == forced["--g-lit"] == "CanvasText"


def test_a_phone_blurs_less_from_where_its_menu_becomes_a_phone_sheet() -> None:
    (phone,) = medias(":root", "_tokens.css", "--g-blur")
    assert phone == medias(".profile-menu[open]", NAV, "background-color", "--g-dim")[0]
    assert float(_root(phone[0])["--g-blur"].removesuffix("px")) < float(
        token("--g-blur").removesuffix("px")
    )
    assert "var(--g-blur)" in token("--g-filter")


def test_the_phone_profile_sheet_dims_the_page_without_a_third_layer_of_glass() -> None:
    for state in (".profile-menu", ".profile-menu[open]"):
        veil = style(state, NAV, "(max-width: 699.98px)")
        assert "backdrop-filter" not in veil and "-webkit-backdrop-filter" not in veil, state
    assert style(".profile-menu[open]", NAV, "(max-width: 699.98px)")["background-color"] == (
        "var(--g-dim)"
    )


def test_the_menu_fades_its_glass_panel_and_not_a_parent_that_would_cut_it_off() -> None:
    # Glass under a parent with opacity below 1 sees none of the page while it fades.
    for state in (".profile-menu", ".profile-menu[open]"):
        assert "opacity" not in style(state, NAV), state
        assert "opacity" not in style(state, NAV, "(max-width: 699.98px)"), state
    assert style(".profile-panel", NAV)["opacity"] == "0"
    assert style(".profile-menu[open] .profile-panel", NAV)["opacity"] == "1"


def test_the_search_s_leaving_hint_is_not_left_drawn_over_the_new_one() -> None:
    # Seen only through its fade (watch/search.js): where the fade does not run, it is hidden.
    assert style(".nav-glass .watch-search__ph.is-old", NAV)["opacity"] == "0"


def test_the_search_s_new_hint_starts_as_the_old_one_all_but_goes_and_soon() -> None:
    js = (CSS_DIR.parent / "js" / "watch" / "search.js").read_text(encoding="utf-8")
    out = int(re.search(r"HINT_OUT_MS = (\d+);", js)[1])
    delay = int(re.search(r"HINT_IN_DELAY_MS = (\d+);", js)[1])
    # Eased out, the old words are all but gone by halfway through their fade: the new ones start
    # no sooner, so the two are never drawn together, and no later than 100 ms, so the field is
    # never empty for long.
    assert out / 2 <= delay <= 100


def test_a_pointer_over_the_way_back_lights_its_glass_and_keeps_the_tint_under_its_word() -> None:
    lit = style(".nav-back:hover", NAV, "(hover: hover)")
    assert lit["background-color"].startswith("color-mix(in srgb, var(--isle),"), lit


def _side_glass() -> tuple[float, ...]:
    """A side panel's glass over a white page, the page under the panel's veil."""
    return _glass_over(_on(token("--g-veil"), WHITE), float(token("--g-a")), filtered=True)


def _fill(value: str) -> str:
    """A fill of the side panels, as their glass resolves it."""
    found = re.fullmatch(r"var\(--(s\d)\)", value.strip())
    assert found, value
    return token(f"--g-{found[1]}")


@pytest.mark.parametrize(("veil", "sheet"), [(".you__veil", YOU), (".space__veil", SPACE)])
def test_a_side_panel_dims_the_page_with_a_plain_veil(veil: str, sheet: str) -> None:
    drawn = style(veil, sheet)
    assert drawn["background"] == "var(--g-veil)"
    # Blurred, it would be one more layer of glass under the panel's.
    assert "backdrop-filter" not in drawn and "-webkit-backdrop-filter" not in drawn


def test_the_pane_s_glass_is_a_layer_of_its_own_that_fades_and_the_pane_does_not() -> None:
    # Blur or a fade on the pane itself would leave every blur inside it (the History tools) nothing
    # of the page to show, and would make the pane the box its fixed menus are placed in.
    for state in (".you__panel", ".you.is-open .you__panel"):
        drawn = style(state, YOU)
        for prop in ("backdrop-filter", "-webkit-backdrop-filter", "opacity", "filter"):
            assert prop not in drawn, (state, prop)
    assert style(".you__panel::before", YOU)["opacity"] == "0"
    assert style(".you.is-open .you__panel::before", YOU)["opacity"] == "1"
    # On a phone the sheet slides in whole.
    assert style(".you__panel::before", YOU, PHONE)["opacity"] == "1"


@pytest.mark.parametrize(
    ("glass", "shown", "sheet"),
    [
        (".space__panel", ".space.is-open .space__panel", SPACE),
        (".watch-keep-menu", ".watch-keep-menu.is-open", KEEP),
    ],
)
def test_a_panel_and_the_keep_menu_fade_their_own_glass(glass: str, shown: str, sheet: str) -> None:
    assert style(glass, sheet)["opacity"] == "0"
    assert style(shown, sheet)["opacity"] == "1"


def test_while_the_pane_changes_width_its_frame_carries_the_glass_once() -> None:
    assert style(".you.is-sizing .you__panel::before", YOU)["visibility"] == "hidden"
    fill = style(".you__frame-fill", YOU)
    assert fill["background"] == "var(--g-regular)"
    assert fill["backdrop-filter"] == fill["-webkit-backdrop-filter"] == "var(--g-filter)"
    # The shadow is the shade's alone, outside the glass's clip, scaled from the side that stays:
    # on the fill it would be clipped and drawn twice, and scaled from the left it would part from
    # the glass mid-step.
    shade = style(".you__frame-shade", YOU)
    assert shade["box-shadow"] == "var(--g-shadow)"
    assert shade["transform-origin"] == "100% 0"
    assert "--g-shadow" not in fill["box-shadow"]
    # Its ends lie over the fill: glass of their own there would be laid twice, and darker.
    ends = style(".you__frame-end", YOU)
    assert "background" not in ends and "backdrop-filter" not in ends


@pytest.mark.parametrize(
    ("menu", "sheet", "media"),
    [
        # As a sheet it covers History's stuck tools, which blur too.
        (".hx-menu", HISTORY, PHONE),
        ("html:is(.has-space, .has-ttl) .watch-keep-menu", KEEP, PHONE),
    ],
)
def test_a_menu_that_opens_on_a_panel_s_glass_is_solid(menu: str, sheet: str, media: str) -> None:
    drawn = style(menu, sheet, media)
    assert drawn["background"] == "var(--g-solid)"
    assert drawn.get("backdrop-filter", "none") == "none"


# A row lit on a card of the side panels, and what is laid open under one: the card's veil and the
# row's, one over the other; or a tile, whose fill is its own card, lit on the glass alone.
LIT = [
    (".hist[open] > .hist-sum", HISTORY, None, True),
    (".hist-sum:active", HISTORY, None, True),
    (".hist-body", HISTORY, None, True),
    (".stx-link:hover", SETTINGS, None, True),
    (".stx-link[aria-current]", SETTINGS, "(min-width: 1000px)", True),
    (":is(a, button).yo-row:hover", PROFILE, "(hover: hover)", True),
    (":is(a, button).yo-row:active", PROFILE, None, True),
    (".yo-pick-row:active", PROFILE, None, True),
    (".setg-pg .intg-card-hd:hover", PANELS, None, True),
    (".dev .btn--danger", PROFILE, None, True),
    (".yo-tile:hover", PROFILE, "(hover: hover)", False),
    (".yo-tile:active", PROFILE, None, False),
    (".yo-wide-btn:not([disabled]):hover", PROFILE, "(hover: hover)", False),
]


@pytest.mark.parametrize(("selector", "sheet", "media", "on_card"), LIT)
def test_a_row_lit_on_a_card_keeps_its_quiet_blue_and_red_words_over_a_white_page(
    selector: str, sheet: str, media: str | None, on_card: bool
) -> None:
    card = _on(token("--g-s2"), _side_glass()) if on_card else _side_glass()
    lit = _on(_fill(style(selector, sheet, media)["background"]), card)
    for words in ("--g-t3", "--g-accent-text", "--g-danger-text"):
        assert contrast(_on(token(words), lit), lit) >= 4.5, words


# A chip tinted with a see-through colour, on a panel's glass.
FLOORED = [
    (".yo-role__pill", PROFILE),
    (".dev-here", PROFILE),
    (".setg-pg .glyph", PANELS),
    (".setg-pg .intg-card-hd .conn", PANELS),
    (".setg-pg .lp-head .lp-glyph", PANELS),
    (".setg-pg .usr-row .you-pill", PANELS),
    (".space-item .watch-keep::before", SPACE),
]


@pytest.mark.parametrize(("chip", "sheet"), FLOORED)
def test_a_tinted_chip_on_a_panel_has_a_floor_under_its_tint(chip: str, sheet: str) -> None:
    assert style(chip, sheet)["box-shadow"] == "var(--g-chip-floor)"


@pytest.mark.parametrize("brand", ["plex", "sonarr", "radarr", "mailgun", "openai", "tmdb", "omdb"])
def test_a_service_s_letters_hold_on_the_lit_head_of_its_open_card(brand: str) -> None:
    letters = re.fullmatch(
        r"color-mix\(in srgb, var\((--[\w-]+)\) (\d+)%, var\(--t1\)\)",
        style(f".setg-pg .gy-{brand}", PANELS)["color"],
    )
    assert letters, brand
    share = int(letters[2]) / 100
    colour = tuple(
        share * c + (1 - share) * w
        for c, w in zip(rgb(token(letters[1])), rgb(token("--t1")), strict=True)
    )
    head = _fill(style(".setg-pg .intg-card-hd:hover", PANELS)["background"])
    row = _on(head, _on(token("--g-s2"), _side_glass()))
    floor = _on(re.search(r"rgb\([^)]*\)", token("--g-chip-floor"))[0], row)
    chip = _on(token(f"--brand-{brand}-bg"), floor)
    assert contrast(colour, chip) >= 4.5


@pytest.mark.parametrize("sheet", [YOU, PROFILE, HISTORY, SETTINGS, PANELS, SPACE])
def test_no_word_or_tick_in_the_side_panels_is_in_the_blue_kept_for_fills_and_rings(
    sheet: str,
) -> None:
    # On glass the fills' blue is under 4.5:1 over a white page; words and ticks take --accent-text.
    for where, selectors, decls in rules(sheet):
        assert decls.get("color") != "var(--accent)", (sheet, where, selectors)


@pytest.mark.parametrize(
    "sheet",
    [KEEP, "_watch_browse.css", "_watch_dlgrid.css", "_watch_grid.css", "_watch_detail.css"],
)
def test_no_word_or_tick_inside_the_keep_menu_is_in_the_blue_kept_for_fills_and_rings(
    sheet: str,
) -> None:
    # Every menu built on the keep menu (picks, all titles, a download's) is its glass.
    inside = [
        (where, sels, decls)
        for where, sels, decls in rules(sheet)
        if any("watch-keep-menu" in sel for sel in sels)
    ]
    assert inside
    for where, selectors, decls in inside:
        assert decls.get("color") != "var(--accent)", (sheet, where, selectors)


def test_every_backdrop_filter_has_an_equal_webkit_one_in_the_same_rule() -> None:
    # Safari reads only the prefixed one: one half alone is no glass there, or no glass elsewhere.
    seen = 0
    for sheet in sorted(path.name for path in CSS_DIR.glob("*.css")):
        for where, selectors, decls in rules(sheet):
            if "backdrop-filter" in decls or "-webkit-backdrop-filter" in decls:
                seen += 1
                assert decls.get("backdrop-filter") == decls.get("-webkit-backdrop-filter"), (
                    sheet,
                    where,
                    selectors,
                )
    assert seen > 20


def test_the_pane_s_glass_paints_over_its_veil_and_under_what_it_holds() -> None:
    # The pane is a layer of its own, so its glass, a layer below what it holds, stays above the
    # veil, which comes before it: without, the veil would darken the glass.
    assert style(".you__panel", YOU)["z-index"] == "0"
    assert style(".you__panel::before", YOU)["z-index"] == "-1"


def test_what_the_pane_holds_fades_with_its_glass_and_never_before_it() -> None:
    for state, shown in ((".you__panel", "0"), (".you.is-open .you__panel", "1")):
        glass = style(state + "::before", YOU)
        held = style(state + " > *", YOU)
        assert glass["opacity"] == held["opacity"] == shown, state
        assert glass["transition"] == held["transition"], state


def test_the_pane_s_views_are_clipped_to_it_so_a_step_never_draws_outside_its_glass() -> None:
    assert style(".you__views", YOU)["overflow"] in ("hidden", "clip")


def test_on_a_phone_the_glass_below_the_sheet_is_the_sheet_s_own_glass() -> None:
    below = style(".you__panel::after", YOU, PHONE)
    sheet = style(".you__panel::before", YOU)
    assert below["background"] == sheet["background"]
    assert below["backdrop-filter"] == sheet["backdrop-filter"] == "var(--g-filter)"


def test_nothing_round_the_space_panel_fades_or_it_would_have_nothing_to_blur() -> None:
    checked = 0
    for where, selectors, decls in rules(SPACE):
        for sel in selectors:
            if re.fullmatch(r"(?:html\S*\s+)?\.space(?:\.[\w-]+|\[[^\]]*\])*", sel):
                checked += 1
                assert "opacity" not in decls and "filter" not in decls, (where, sel)
    assert checked


def test_the_space_list_fades_into_its_foot_where_scroll_timelines_run_and_where_they_do_not() -> (
    None
):
    live = style(".space-pane", SPACE, "@supports (animation-timeline: scroll())")
    assert "var(--space-fade)" in live["mask-image"]
    assert live["animation-timeline"] == "scroll(self)"
    still = style(".space-pane", SPACE, "@supports not (animation-timeline: scroll())")
    assert still["-webkit-mask-image"] == still["mask-image"]
    fade = px(
        re.fullmatch(r"linear-gradient\(to top, transparent, #000 (\S+)\)", still["mask-image"])[1]
    )
    # The list's end is clear of the fade, so its last words are never faded out.
    assert px(still["padding-bottom"]) >= fade >= 32


def _keyframes(sheet: str, name: str) -> str:
    found = re.search(rf"@keyframes {name}\s*\{{(.*?)\}}\s*\}}", text(sheet), flags=re.S)
    assert found, name
    return found[1]


def test_a_space_row_that_goes_is_cut_away_as_the_rows_below_slide_over_it() -> None:
    # The rows below draw no fill: the row that goes is cut from its foot up on their own timing
    # (watch/core.js flip), so their words never lie over its words.
    core = (CSS_DIR.parent / "js" / "watch" / "core.js").read_text(encoding="utf-8")
    flip = re.search(
        r"function flip.*?duration: tokenMs\('(--[\w-]+)'.*?easing: token\('(--[\w-]+)'", core, re.S
    )
    name, duration, easing, *_ = style(".space-item.is-gone", SPACE)["animation"].split()
    assert (duration, easing) == (f"var({flip[1]})", f"var({flip[2]})")
    assert "clip-path: inset(0 0 100% 0)" in _keyframes(SPACE, name)


def test_an_opening_row_s_details_are_uncovered_level_with_the_rows_that_slide_down() -> None:
    # history.js slides the rows below by the details' height on the details' own animation: the
    # details show from their top down as the rows go, and a shift would move their foot off them.
    js = (CSS_DIR.parent / "js" / "history.js").read_text(encoding="utf-8")
    assert "style.animationDuration" in js and "style.animationTimingFunction" in js
    shut, whole = "clip-path: inset(0 0 100% 0)", "clip-path: inset(0)"
    for name, first, last in (("hist-open", shut, whole), ("hist-close", whole, shut)):
        frames = _keyframes(HISTORY, name)
        assert re.search(r"from \{[^}]*" + re.escape(first), frames), name
        assert re.search(r"to \{[^}]*" + re.escape(last), frames), name
        assert "transform" not in frames, name


@pytest.mark.parametrize("head", [".hx-tools", ".hx-day__top"])
def test_a_stuck_history_head_only_blurs_the_pane_s_glass_and_eases_in(head: str) -> None:
    stuck = style(head + ".is-stuck", HISTORY)
    # The pane's glass is already darkened once: a tint or the glass's filter again is a dark band.
    assert re.fullmatch(r"blur\(var\(--g-blur\)\)", stuck["backdrop-filter"])
    assert _colour(stuck["background-color"], {})[3] == 0
    # Where glass is not drawn, nothing blurs: the glass's own fill covers what passes under.
    for where in (NO_BLUR, "(prefers-reduced-transparency: reduce)", "(prefers-contrast: more)"):
        assert _root(where)["--g-stuck"] == "var(--g-regular)", where
    assert _root("(forced-colors: active)")["--g-stuck"] == "Canvas"
    for media in (None, PHONE):
        try:
            rest = style(head, HISTORY, media)
        except AssertionError:
            continue
        if "transition" in rest:
            for prop in ("backdrop-filter", "-webkit-backdrop-filter"):
                assert re.search(rf"(?<![\w-]){prop} var\(--dur", rest["transition"]), (media, prop)


def test_a_form_s_foot_blurs_what_scrolls_under_it_and_fades_in_before_its_button() -> None:
    foot = style(".yo-foot", PROFILE)
    assert foot["backdrop-filter"] == foot["-webkit-backdrop-filter"] == "blur(var(--g-blur))"
    assert foot["background"] == "var(--g-stuck)"
    assert foot["-webkit-mask-image"] == foot["mask-image"]
    fade = re.fullmatch(r"linear-gradient\(transparent, #000 (\S+)\)", foot["mask-image"])
    assert fade and px(fade[1]) <= px(foot["padding"].split()[0])
    # A phone on its side: the foot follows the form, so it neither blurs nor fades what it covers.
    flat = style(".yo-foot", PROFILE, "(max-height: 500px)")
    for prop in ("backdrop-filter", "-webkit-backdrop-filter", "mask-image", "-webkit-mask-image"):
        assert flat[prop] == "none", prop


@pytest.mark.parametrize(("badge", "sheet"), [(".yo-pic__badge", PROFILE), (".hx-act", HISTORY)])
def test_a_badge_on_a_picture_is_solid_and_edged_with_a_hairline(badge: str, sheet: str) -> None:
    drawn = style(badge, sheet)
    # Read as the pane's glass reads it, where the fills are veils.
    assert _colour(drawn["background"], style(".you__panel", YOU))[3] == 1
    # Its edge is the glass's own hairline, never a ring in the tint, which reads black there.
    assert drawn["box-shadow"] == "0 0 0 1px var(--g-edge)"


@pytest.mark.parametrize("sheet", [YOU, PROFILE, HISTORY, SETTINGS, PANELS, SPACE, KEEP])
def test_the_glass_s_tint_is_never_drawn_whole_as_a_ring(sheet: str) -> None:
    # Alone it is far darker than the glass it tints: a black ring there.
    for where, selectors, decls in rules(sheet):
        for prop in ("border", "border-color", "outline", "outline-color", "box-shadow"):
            assert "rgb(var(--g-tint))" not in decls.get(prop, ""), (where, selectors, prop)


def test_the_way_back_s_blue_word_keeps_4_5_to_1_at_rest_under_the_pointer_and_pressed() -> None:
    glass = _side_glass()
    for state, media in (
        (".you__back", None),
        (".you__back:hover", "(hover: hover)"),
        (".you__back:active", None),
    ):
        fill = style(state, YOU, media).get("background")
        under = _on(_fill(fill), glass) if fill else glass
        assert contrast(_on(token("--g-accent-text"), under), under) >= 4.5, state


def _open_body() -> tuple[float, ...]:
    card = _on(token("--g-s2"), _side_glass())
    return _on(_fill(style(".hist-body", HISTORY)["background"]), card)


def test_an_open_row_s_buttons_keep_their_words_over_a_white_page_under_the_pointer() -> None:
    for state in (".hx-acts .btn", ".hx-acts .btn:hover"):
        lit = _on(_fill(style(state, HISTORY)["background"]), _open_body())
        assert contrast(_on(token("--t1"), lit), lit) >= 4.5, state


@pytest.mark.parametrize(
    ("mark", "media", "lit"),
    [
        (".hx mark", None, False),
        (".hist[open] mark", None, True),
        (".hist-sum:active mark", None, True),
        (".hist-sum:hover mark", "(hover: hover)", True),
    ],
)
def test_a_found_word_keeps_4_5_to_1_on_its_mark_over_a_white_page(
    mark: str, media: str | None, lit: bool
) -> None:
    # A lit row and an open row's details lie one veil over the card: the mark there is fainter.
    under = _open_body() if lit else _on(token("--g-s2"), _side_glass())
    marked = _over(_colour(style(mark, HISTORY, media)["background"], {}), under)
    assert contrast(_on(token("--t1"), marked), marked) >= 4.5


def test_a_pressed_keep_menu_item_keeps_its_red_word_over_a_white_page() -> None:
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    pressed = _on(_fill(style(".watch-keep-menu__item:active", KEEP)["background"]), glass)
    assert contrast(_on(token("--g-danger-text"), pressed), pressed) >= 4.5


@pytest.mark.parametrize(
    ("scope", "sheet"),
    [
        (".you__panel :focus-visible", YOU),
        (".space__panel :focus-visible", SPACE),
        (".profile-panel :focus-visible", NAV),
        (".nav-end :focus-visible", NAV),
        (".ui-toast :focus-visible", TOASTS),
        (".login-card :focus-visible", LOGIN),
    ],
)
def test_the_keyboard_s_ring_on_a_glass_surface_takes_the_glass_s_ring_colour(
    scope: str, sheet: str
) -> None:
    assert style(scope, sheet)["outline-color"] == "var(--g-focus)"


def test_the_glass_s_ring_colour_stands_3_to_1_off_glass_over_a_white_and_a_black_page() -> None:
    ring = _colour("var(--g-focus)", {})
    assert ring[3] == 1
    alpha = float(token("--g-a"))
    for page in (WHITE, BLACK):
        for glass in (
            _glass_over(page, alpha, filtered=True),
            _glass_over(_on(token("--g-veil"), page), alpha, filtered=True),
        ):
            assert contrast(ring[:3], glass) >= 3, page


@pytest.mark.parametrize("sheet", [YOU, PROFILE, HISTORY, SETTINGS, PANELS, SPACE])
def test_every_ring_the_side_panels_draw_stands_3_to_1_off_their_glass_over_a_white_page(
    sheet: str,
) -> None:
    glass = _side_glass()
    for where, selectors, decls in rules(sheet):
        if any("forced-colors" in w for w in where) or not any(":focus" in s for s in selectors):
            continue
        for prop in ("outline", "outline-color"):
            colour = re.search(r"var\(--[\w-]+\)", decls.get(prop, ""))
            if colour:
                ring = _colour(colour[0], {})
                assert contrast(_over(ring, glass), glass) >= 3, (where, selectors, prop)


@pytest.mark.parametrize("kind", ["movies", "tv", "anime", "other", "free"])
def test_each_kind_s_mark_in_the_space_key_stands_3_to_1_off_the_panel_s_glass(kind: str) -> None:
    scope = style(".space__panel", SPACE)
    glass = _side_glass()
    fill = _over(_colour(style(f".space-c--{kind}", SPACE)["--c"], scope), glass)
    mark = fill
    for where, selectors, decls in rules(SPACE):
        if f".space-c--{kind} .space-dot" in selectors and not where:
            edge = re.fullmatch(r"inset 0 0 0 [\d.]+px (.+)", decls["box-shadow"])
            mark = _over(_colour(edge[1], scope), fill)
    assert contrast(mark, glass) >= 3


def test_the_bar_s_free_part_has_the_same_edge_as_its_key() -> None:
    edge = style(".space-seg--free::before", SPACE)["box-shadow"]
    assert edge == style(".space-c--free .space-dot", SPACE)["box-shadow"]


def test_stuck_tools_hold_their_field_s_and_pills_words_over_the_cards_passing_under() -> None:
    # The band only blurs: what lies under the field and the pills is a card, lighter than the glass.
    floored = style(
        '.hx-tools.is-stuck :is(.hx-search input, .hx-group:not([aria-pressed="true"]), .hx-chip)',
        HISTORY,
    )
    assert floored["box-shadow"] == "var(--g-chip-floor)"
    card = _on(token("--g-s2"), _side_glass())
    floor = _on(re.search(r"rgb\([^)]*\)", token("--g-chip-floor"))[0], card)
    for control, words in ((".hx-search input", "--g-t3"), (".hx-group", "--g-t2")):
        veil = _on(_fill(style(control, HISTORY)["background"]), floor)
        assert contrast(_on(token(words), veil), veil) >= 4.5, control


def test_the_bar_island_s_controls_draw_their_ring_on_its_glass_in_the_glass_s_ring_colour() -> (
    None
):
    meter = style(".space-meter:focus-visible", SPACE)
    width, _, colour = meter["outline"].split()
    assert colour == "var(--g-focus)"
    # The island ends at the controls' edges: a ring outside them would lie on the page.
    for offset in (
        meter["outline-offset"],
        style(".profile-btn:focus-visible", NAV)["outline-offset"],
    ):
        assert px(offset) + px(width) <= 0, offset


def test_below_900px_the_profile_button_s_ring_and_its_download_ring_keep_glass_between() -> None:
    narrow = "(max-width: 899.98px)"
    # Below 900px the button is a disc round the picture, its ring inside its edge.
    edge = px(style(".profile-btn", NAV)["min-width"]) / 2
    width = px(style(".space-meter:focus-visible", SPACE)["outline"].split()[0])
    ring_in = edge + px(style(".profile-btn:focus-visible", NAV)["outline-offset"])
    assert ring_in + width <= edge
    # The download ring, drawn round the picture, draws in while the keyboard's ring shows.
    picture = px(style(".avatar", NAV)["width"]) / 2
    band = float(re.search(r"calc\(100% - ([\d.]+)px\)", style(".profile-ring", NAV)["mask"])[1])
    assert style(".profile-btn:focus-visible .profile-ava", NAV, narrow)["transform"] == "none"
    drawn = style(".profile-btn:focus-visible .profile-ring", NAV, narrow)
    assert "transform" not in drawn
    outer = picture - px(drawn["inset"])
    assert outer + 1.5 <= ring_in
    # Not onto the picture.
    assert outer - band >= picture


def test_a_stuck_day_head_holds_its_words_over_a_white_picture_blurred_under_it() -> None:
    # The worst a row can pass under the head: a white picture filling an avatar's disc, as much of
    # it as the head's blur gathers at the disc's middle, which is most where the blur is least.
    radius = px(style(".hx-who .avatar", HISTORY)["width"]) / 2
    sigma = min(px(_root()["--g-blur"]), px(_root(PHONE)["--g-blur"]))
    share = 1 - math.exp(-(radius**2) / (2 * sigma**2))
    card = _on(token("--g-s2"), _side_glass())
    bright = tuple(share * w + (1 - share) * c for w, c in zip(WHITE, card, strict=True))
    floor = re.fullmatch(
        r"var\((--[\w-]+)\)", style(".hx-day__top.is-stuck", HISTORY)["box-shadow"]
    )
    under = _on(re.search(r"rgb\([^)]*\)", token(floor[1]))[0], bright)
    for words in ("--t1", "--g-t3"):
        assert contrast(_on(token(words), under), under) >= 4.5, words


def test_a_phone_never_stacks_a_third_blur_in_history() -> None:
    # Unfolded, the tools lie over a stuck day head: one of the two blurs, the other is under it.
    # Pushed up by the next day's head, the stuck one slides under the tucked tools: the same.
    for head in (
        ".hx-tools.has-day:not(.is-tucked) ~ .hx-list .hx-day__top.is-stuck",
        ".hx-day__top.is-pushed",
    ):
        covered = style(head, HISTORY, PHONE)
        assert covered["backdrop-filter"] == covered["-webkit-backdrop-filter"] == "none", head
        # At once: a blur easing out would be a third layer while it lasts.
        assert "backdrop-filter" not in covered["transition-property"], head
    # What shows of a pushed head is solid, at once: the rows under it would show sharp through it.
    pushed = style(".hx-day__top.is-pushed", HISTORY, PHONE)
    assert pushed["background-color"] == "var(--g-solid)"
    assert "background-color" not in pushed["transition-property"]
    # The kinds' arrows sit on the stuck tools: solid, never a blur of their own.
    for where, selectors, decls in rules(HISTORY):
        if any(".hx-kinds-arrow" in s for s in selectors):
            assert decls.get("backdrop-filter", "none") == "none", (where, selectors)
            assert decls.get("-webkit-backdrop-filter", "none") == "none", (where, selectors)
    for arrow in (".hx-kinds-arrow", ".hx-kinds-arrow:hover"):
        drawn = style(arrow, HISTORY, "(hover: hover) and (pointer: fine)")
        assert _colour(drawn["background"], {})[3] == 1, arrow


def _matches(condition: str, w: int, h: int) -> bool:
    """Whether a screen of *w* by *h* meets an @media condition of widths and heights alone."""
    limits = {
        "min-width": lambda v: w >= v,
        "max-width": lambda v: w <= v,
        "max-height": lambda v: h <= v,
    }
    for alternative in _top_level(condition, ","):
        parts = re.findall(r"\(([\w-]+):\s*([\d.]+)px\)", alternative)
        if (
            parts
            and len(parts) == alternative.count("(")
            and all(limits[f](float(v)) for f, v in parts)
        ):
            return True
    return False


def _at(selector: str, sheet: str, w: int, h: int) -> dict[str, str]:
    """What the rules headed by *selector* declare on a screen of *w* by *h*, later winning."""
    merged: dict[str, str] = {}
    for where, selectors, decls in rules(sheet):
        if selector in selectors and all(
            not c.startswith("@") and _matches(c, w, h) for c in where
        ):
            merged.update(decls)
    return merged


def _length(value: str, w: int, scope: dict[str, str]) -> float:
    """A length in px on a screen *w* wide with no safe areas, *scope* giving the custom properties."""
    for _ in range(4):
        value = re.sub(r"env\([^()]*\)", "0px", value)
        value = re.sub(
            r"var\((--[\w-]+)(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)",
            lambda m: scope.get(m[1], m[2] or ""),
            value,
        )
    value = value.replace("100vw", f"{w}px").replace("100%", f"{w}px").replace("calc", "")
    value = value.replace("px", "")
    # Numbers, signs, brackets and min/max alone are left by now: anything else is not a length.
    assert re.fullmatch(r"(?:[\d.\s+\-*/(),]|min|max)+", value), value
    return float(eval(value, {"__builtins__": {}, "min": min, "max": max}))


# Phones upright and on their side, a small window, a desktop, and the edges of their rules.
SCREENS = [(402, 874), (440, 956), (667, 375), (700, 420), (874, 402), (1024, 560), (1440, 900)]


@pytest.mark.parametrize(("w", "h"), SCREENS)
def test_a_toast_over_a_form_never_lies_on_its_controls_whatever_its_scroll(w: int, h: int) -> None:
    toast = _at("html.has-you-form body .ui-toast-host", PROFILE, w, h)
    foot = style(".yo-foot", PROFILE)
    pinned = _at(".yo-foot", PROFILE, w, h)["position"] != "static"
    if w >= 700:
        # A side panel: the form scrolls under any spot on it, so the toast stands off it.
        pane = {**style(".you", YOU), **_at(".you__panel", YOU, w, h)}
        left = w - _length(pane["right"], w, {}) - _length(pane["width"], w, pane)
        assert toast["right"] == "auto"
        assert _length(toast["left"], w, {}) + _length(toast["max-width"], w, {}) <= left
    elif pinned:
        # A sheet with its foot held in sight: the toast stands above it, over what never scrolls.
        top, _, bottom = _top_level(foot["padding"], " ")
        button = px(style(".yo-foot .btn", PROFILE)["min-height"])
        tall = _length(top, w, {}) + button + _length(bottom, w, {})
        assert toast["top"] == "auto" and _length(toast["bottom"], w, {}) > tall
    else:
        # The foot follows the form: the toast stands on the sheet's bar, under its handle and
        # between Back and Close, which take the bar's two equal sides.
        sheet_top = re.search(
            r"max\([^()]*(?:\([^()]*\))?[^()]*\)", style(".you__panel", YOU, PHONE)["height"]
        )[0]
        grab = px(style(".you__grab", YOU, PHONE)["height"])
        assert toast["bottom"] == "auto"
        assert _length(toast["top"], w, {}) >= _length(sheet_top, w, {}) + grab
        assert _length(toast["left"], w, {}) == _length(toast["right"], w, {}) > 44


def test_the_pane_s_glass_reaches_to_its_edge_so_what_it_holds_sits_inside_it_evenly() -> None:
    glass = style(".you__panel::before", YOU)
    assert style(".you__panel", YOU)["padding"] == re.match(r"(\S+) solid", glass["border"])[1]
    phone = style(".you__panel::before", YOU, PHONE)["border-width"]
    assert style(".you__panel", YOU, PHONE)["padding"] == phone


def test_while_the_pane_changes_width_its_glass_keeps_the_pane_s_rounded_corners() -> None:
    radius = style(".you__panel", YOU)["border-radius"]
    clip = style(".you__frame-glass", YOU)
    # The fill slides under this clip, which rounds its right corners as the pane's.
    assert clip["overflow"] == "hidden" and clip["border-radius"] == radius
    assert style(".you__frame-fill", YOU)["border-radius"] == f"{radius} 0 0 {radius}"
    assert style(".you__frame-shade", YOU)["border-radius"] == radius


def test_an_open_row_s_button_lights_under_the_pointer() -> None:
    rest = _colour(_fill(style(".hx-acts .btn", HISTORY)["background"]), {})
    lit = _colour(_fill(style(".hx-acts .btn:hover", HISTORY)["background"]), {})
    assert lit[3] > rest[3]


def test_a_filter_menu_option_under_the_pointer_keeps_its_quiet_count_over_a_white_page() -> None:
    assert style(".hx-menu", HISTORY)["backdrop-filter"] == "var(--g-filter)"
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    lit = _on(_fill(style(".hx-opt:hover", HISTORY)["background"]), glass)
    assert contrast(_on(token("--g-t3"), lit), lit) >= 4.5


def test_no_view_s_fade_is_forced_over_the_step_s_own() -> None:
    for where, selectors, decls in rules(YOU):
        if any(".you-view" in s for s in selectors):
            assert "!important" not in decls.get("opacity", ""), (where, selectors)


def _on_tint_over_white_glass(tint: str) -> tuple[float, ...]:
    return _on(_resolve(tint), _glass_over(WHITE, float(token("--g-a")), filtered=True))


@pytest.mark.parametrize(
    ("message", "need"), [(".login-msg", 4.5), (".login-msg.is-wait", 4.7), (".fpc-eyebrow", 4.7)]
)
def test_the_sign_in_card_s_red_and_amber_words_hold_on_their_tints_over_a_white_page(
    message: str, need: float
) -> None:
    # Amber small words get a margin over 4.5: the browser's blur lands a little under the model.
    drawn = style(message, LOGIN)
    word = re.fullmatch(r"var\((--[\w-]+)\)", drawn["color"])[1]
    lifted = _resolve(style(".login-card", LOGIN)[word])
    under = _on_tint_over_white_glass(drawn["background"])
    assert contrast(_on(lifted, under), under) >= need, (message, lifted)


@pytest.mark.parametrize(
    "surface",
    [
        (".login-card", LOGIN),
        (EPISODES, DL_PAGE),
        (".watch-all__head", GRID),
        (".dlg__head", DL_GRID),
    ],
)
def test_rings_on_glass_take_the_lifted_blue_and_keep_3_to_1_over_a_white_page(
    surface: tuple[str, str],
) -> None:
    assert style(*surface)["--accent"] == "var(--g-accent)"
    # --g-accent is worked out where the tokens declare it, from the page's own blue.
    blue = _colour(token("--g-accent"), {"--accent": token("--accent")})
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    assert contrast(_over(blue, glass), glass) >= 3


def test_every_ring_the_error_card_draws_is_the_glass_s_ring_colour() -> None:
    checked = 0
    for where, selectors, decls in rules("_error.css"):
        if any("forced-colors" in w for w in where) or not any(":focus" in s for s in selectors):
            continue
        for prop in ("outline", "outline-color"):
            colour = re.search(r"var\(--[\w-]+\)", decls.get(prop, ""))
            if colour:
                checked += 1
                assert colour[0] == "var(--g-focus)", (selectors, prop)
    assert checked


def test_the_sign_in_field_s_drawn_ring_is_the_glass_s_ring_colour() -> None:
    # The field's ring is drawn by its box, over an outline made transparent.
    ring = style(".login-box::after", LOGIN)["box-shadow"]
    assert ring.startswith("inset 0 0 0 2px var(--g-focus),"), ring


def test_a_new_password_s_field_on_the_card_takes_the_glass_s_ring_and_a_wrong_one_its_red() -> (
    None
):
    # The password form's own sheet rings its field in the page's blue, stronger than the card's
    # every-ring rule.
    field = '.login-card .pwd .inp:not([aria-invalid="true"]):focus-visible'
    assert style(field, LOGIN)["outline-color"] == "var(--g-focus)"


@pytest.mark.parametrize(("state", "veil"), [("", "--g-s2"), (":focus", "--g-s3")])
def test_a_field_the_browser_fills_in_on_the_card_is_masked_opaque_as_the_card_draws_it(
    state: str, veil: str
) -> None:
    mask = style(f".login-card :is(.pwd-box > .inp, .login-field input):autofill{state}", LOGIN)
    colour = mask["box-shadow"].split("99px ", 1)[1]
    colour = colour.replace("calc(var(--g-a) * 100%)", f"{float(token('--g-a')) * 100}%")
    drawn = _colour(colour, {})
    assert drawn[3] == 1, "the browser's own fill shows through"
    # The field, empty or focused: its veil over the card over a dark page.
    field = _on(token(veil), _glass_over((0.0, 0.0, 0.0), float(token("--g-a")), True))
    assert all(abs(a - b) < 1 for a, b in zip(drawn[:3], field, strict=True)), (drawn, field)


def test_the_sign_in_card_s_link_is_a_word_lifted_for_glass() -> None:
    assert style(".security-note a", LOGIN)["color"] == "var(--accent-text)"


def test_a_state_chip_on_the_episodes_glass_has_a_dark_floor_under_its_tint() -> None:
    assert style(".share-eps .dl-ep-status-pill", DL_PAGE)["box-shadow"] == "var(--g-chip-floor)"


def test_the_ready_chip_on_the_episodes_glass_is_a_solid_green_with_words_that_hold() -> None:
    chip = style(".share-eps .dl-ep-status-pill.ready", DL_PAGE)
    fill = _colour(chip["background"], {})
    assert fill[3] == 1, "a see-through green turns dull under the chip's floor"
    assert chip["box-shadow"] == "none", "the floor every chip takes would dull the green"
    assert contrast(_colour(chip["color"], {}), fill) >= 4.5


def test_the_episodes_glass_takes_its_border_out_of_its_padding_so_no_row_moves() -> None:
    panel = style(EPISODES, DL_PAGE)
    border = px(panel["border"].split()[0])
    assert px(panel["padding"]) + border == px(style(".dl-hero-episodes", "_dl.css")["padding"])


def test_a_pointer_over_the_download_page_s_button_eases_light_over_the_tint() -> None:
    # Only for a pointer that hovers: on touch, a tap would leave it lit.
    assert ":hover" not in "".join(s for w, sels, _ in rules(SHARE) if not w for s in sels)
    lit = style(".share-bar .btn:hover", SHARE, "(hover: hover)")
    assert lit["background-color"].startswith("color-mix(in srgb, var(--g-thin),"), lit


def test_the_trailer_fades_the_bar_s_parts_and_not_the_bar_that_holds_their_glass() -> None:
    # All of the bar is drawn in one sheet, so no later rule of another can undo the fade.
    assert not any(".share-bar" in s for _, sels, _ in rules(DL_PAGE) for s in sels)
    playing = ".share-bar:has(~ main .is-trailer)"
    assert "opacity" not in style(playing, SHARE)
    assert style(f"{playing} > *", SHARE)["opacity"] == "0"
    moves = style(".share-bar .btn", SHARE)["transition"]
    # The button's own moves, and the fade.
    assert moves.startswith("var(--btn-moves),") and "opacity" in moves, moves
    assert style(".btn", "_buttons.css")["transition"] == "var(--btn-moves)"


@pytest.mark.parametrize(
    ("head", "sheet"),
    [(".dlg__head", DL_GRID), (".watch-all__head", GRID)],
)
def test_a_grid_s_sticky_head_is_glass_with_its_words_lifted_for_it(head: str, sheet: str) -> None:
    glass = style(head.replace("__head", "__glass"), sheet)
    assert glass["background"] == "var(--g-thin)"
    assert glass["backdrop-filter"] == glass["-webkit-backdrop-filter"] == "var(--g-filter)"
    words = style(head, sheet)
    under = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    for name in ("--t2", "--t3", "--t4"):
        assert contrast(_on(_resolve(words[name]), under), under) >= 4.5, (head, name)


# Each control on a grid's head: the sheet that draws it, at rest and then in each lit state.
HEAD_CONTROLS = [
    (
        ".watch-all__head",
        GRID,
        "_watch_browse.css",
        ".row-tool",
        (":hover", '[aria-expanded="true"]', ':not([aria-disabled="true"]):active'),
    ),
    (".watch-all__head", GRID, GRID, ".watch-all__back", (":hover", ":active")),
    (".dlg__head", DL_GRID, DL_GRID, ".dlg__back", (":hover", ":active")),
]


@pytest.mark.parametrize(("head", "head_sheet", "sheet", "control", "lit"), HEAD_CONTROLS)
@pytest.mark.parametrize("page", [WHITE, (0.0, 0.0, 0.0)], ids=["white", "black"])
def test_a_lit_control_on_a_grid_s_head_shows_lighter_than_at_rest_with_words_that_hold(
    head: str,
    head_sheet: str,
    sheet: str,
    control: str,
    lit: tuple[str, ...],
    page: tuple[float, ...],
) -> None:
    scope = style(head, head_sheet)
    glass = _glass_over(page, float(token("--g-a")), filtered=True)
    rest = style(control, sheet)
    at_rest = _over(_colour(rest.get("background", "transparent"), scope), glass)
    for state in lit:
        drawn = {**rest, **style(control + state, sheet)}
        under = _over(_colour(drawn["background"], scope), glass)
        # A solid grey over glass on a bright page shows darker than the glass, not lighter.
        assert sum(under) > sum(at_rest) and contrast(under, at_rest) >= 1.1, control + state
        words = _over(_colour(drawn.get("color", "var(--t1)"), scope), under)
        assert contrast(words, under) >= 4.5, (control + state, round(contrast(words, under), 2))


@pytest.mark.parametrize(
    ("glass", "sheet"), [(".watch-all__glass", GRID), (".dlg__glass", DL_GRID)]
)
def test_in_forced_colours_a_grid_s_head_is_always_a_boxed_surface(glass: str, sheet: str) -> None:
    forced = style(glass, sheet, "(forced-colors: active)")
    assert forced["opacity"] == "1" and forced["border-bottom-color"] == "CanvasText", forced


def test_the_share_note_s_words_hold_on_its_card_over_a_white_page() -> None:
    word = style(".share-note p", SHARE)["color"]
    lifted = _colour(word, style(".login-card", LOGIN))
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    assert contrast(_over(lifted, glass), glass) >= 4.5
