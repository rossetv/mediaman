"""Glass: one material named once in the tokens, drawn by the top bar's islands, the profile menu
and toasts, legible over a white page, and solid wherever glass cannot or should not be drawn."""

from __future__ import annotations

import re

import pytest

from tests.unit.web._css import CSS_DIR, contrast, rgb, rules, style, token

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


def test_the_bar_s_islands_are_glass_of_the_thin_weight() -> None:
    bar = style(".nav-glass", NAV)
    assert bar["--isle"] == "var(--g-thin)"
    assert bar["--isle-blur"] == "var(--g-filter)"
    assert bar["--isle-edge"] == "var(--g-edge)" and bar["--isle-lit"] == "var(--g-lit)"
    for island in (".nav-back", ".nav-end"):
        drawn = style(island, NAV)
        assert drawn["background"] == "var(--isle)"
        assert drawn["backdrop-filter"] == drawn["-webkit-backdrop-filter"] == "var(--isle-blur)"


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


def test_the_toast_s_button_is_a_word_lifted_for_glass() -> None:
    assert style(".ui-toast__action", TOASTS)["color"] == "var(--accent-text)"


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


def test_a_phone_blurs_less() -> None:
    phone = _root("(max-width: 599.98px)")
    assert float(phone["--g-blur"].removesuffix("px")) < float(token("--g-blur").removesuffix("px"))
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


def test_the_search_s_new_hint_comes_in_only_once_the_old_one_has_gone() -> None:
    js = (CSS_DIR.parent / "js" / "watch" / "search.js").read_text(encoding="utf-8")
    body = js[js.index("function setHint(") :]
    body = body[: body.index("\n    }\n")]
    out = re.search(r"play\(old, \[[^\]]*\], (\d+)", body)
    into = re.search(r"play\(hintText, \[[^\]]*\], \d+,\s*\{ delay: (\d+)", body)
    assert out and into
    assert int(into[1]) >= int(out[1])


def test_a_pointer_over_the_way_back_lights_its_glass_and_keeps_the_tint_under_its_word() -> None:
    lit = style(".nav-back:hover", NAV, "(hover: hover)")
    assert lit["background-color"].startswith("color-mix(in srgb, var(--isle),"), lit
