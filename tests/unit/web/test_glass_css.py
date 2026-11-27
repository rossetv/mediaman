"""Glass: one material named once in the tokens, drawn by the top bar's islands, the profile menu
and toasts, legible over a white page, and solid wherever glass cannot or should not be drawn."""

from __future__ import annotations

import colorsys
import re

import pytest

from tests.unit.web._css import CSS_DIR, _top_level, contrast, medias, rgb, rules, style, token

NAV = "_nav.css"
TOASTS = "_ui_feedback.css"
NO_BLUR = "@supports not"
WHITE = (255.0, 255.0, 255.0)

# Each surface, the sheet that draws it, and the custom properties it draws its glass with.
SURFACES = [
    (".profile-panel", NAV, "background"),
    (".ui-toast", TOASTS, "background-color"),
]
# What sits on the glass of each surface: what its words, fills and blue and red words resolve to.
ON_GLASS = [
    (".nav-end", NAV),
    (".nav-back", NAV),
    (".nav-glass .watch-search", NAV),
    (".profile-panel", NAV),
    (".ui-toast", TOASTS),
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
    blue = _colour(style(".nav-glass.is-scrolled .brand .wm-man", NAV)["stroke"], {})
    glass = _glass_over(WHITE, float(token("--g-a")), filtered=True)
    assert contrast(_over(blue, glass), glass) >= 4.5
    hue = colorsys.rgb_to_hsv(*(c / 255 for c in blue[:3]))[0]
    brand = colorsys.rgb_to_hsv(*(c / 255 for c in rgb(token("--accent"))))[0]
    assert abs(hue - brand) * 360 < 10


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
