"""Smoke tests for the cinematic-dark redesign.

These tests assert that the CSS and key templates expose the
primitives introduced by the redesign. They do not render HTML or
exercise routes — they are guardrails so that a template refactor
doesn't silently delete the new tokens / classes.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CSS_DIR = REPO / "src/mediaman/web/static/css"
TEMPLATES = REPO / "src/mediaman/web/templates"


def _css() -> str:
    """Concatenate the modular CSS files into a single string.

    The cinematic-dark redesign ships its styles as `static/css/_*.css`
    fragments loaded individually from `base.html`. These tests treat the
    union of those fragments as the contract surface — the same surface
    the browser sees once every link tag has loaded.
    """
    parts = [path.read_text(encoding="utf-8") for path in sorted(CSS_DIR.glob("_*.css"))]
    return "\n".join(parts)


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_design_tokens_defined():
    """Every page composes from these tokens — they must be present."""
    css = _css()
    tokens = (
        # Surfaces
        "--bg",
        "--s1",
        "--s2",
        "--s3",
        "--s4",
        "--hair",
        "--hair-strong",
        # Text
        "--t1",
        "--t2",
        "--t3",
        "--t4",
        # Accents
        "--accent",
        "--accent-cta",
        "--accent-hi",
        # Semantic
        "--success",
        "--warning",
        "--danger",
        "--orange",
        "--purple",
        # Radii
        "--r-xs",
        "--r-sm",
        "--r-md",
        "--r-lg",
        "--r-pill",
        # Layout
        "--pad",
        "--section",
        "--nav-h",
        # Shadow (single allowed)
        "--shadow-card",
    )
    for token in tokens:
        assert token in css, f"missing token: {token}"


def test_legacy_tokens_removed():
    """Tokens from the previous design system must NOT linger."""
    css = _css()
    legacy = (
        "--card-radius",
        "--pill-radius",
        "--radius-sm:",
        "--radius-md:",
        "--radius-lg:",
        "--bp-sm",
        "--bp-md",
        "--bp-lg",
        "--bp-xl",
        "--tab-h",
        "--topbar-h",
        "--container-pad",
        "--space-section",
        "--space-card",
        "--page-header-mb",
    )
    for token in legacy:
        assert token not in css, f"legacy token must be removed: {token}"


def test_fluid_type_uses_clamp():
    """Headlines and stat numbers scale fluidly between viewports."""
    css = _css()
    # Each primitive must use clamp() for its font-size — catches silent
    # reverts to fixed px.
    fluid_selectors = (
        ".ph h1 {",  # page headline — standalone rule, no trailing comma
        ".dl-hero-title",  # downloads cinematic hero
    )
    for sel in fluid_selectors:
        idx = css.find(sel)
        assert idx >= 0, f"selector not found: {sel}"
        block = css[idx : idx + 600]
        assert "clamp(" in block, f"expected clamp() near {sel!r}"


def test_nav_surfaces_present():
    """The nav is one bar of islands, .nav-glass: the way back (or the search), the island at
    the end, .nav-end, on which the profile button stands, and the .profile-menu."""
    css = _css()
    for sel in (
        ".nav-glass",
        ".nav-back",
        ".nav-end",
        ".profile-btn",
        ".profile-ring",
        ".profile-count",
        ".profile-menu",
        ".profile-panel",
        ".profile-item",
    ):
        assert sel in css, f"missing nav selector: {sel}"


def test_the_foot_of_a_phone_page_and_its_sheet_clear_the_home_bar():
    nav = (CSS_DIR / "_nav.css").read_text()
    phone = nav.split("@media (max-width: 699.98px) {")
    assert sum("env(safe-area-inset-bottom" in block.split("\n}")[0] for block in phone[1:]) >= 2


def test_component_primitives_present():
    """Every page composes from these primitives — verify they exist."""
    css = _css()
    primitives = (
        ".btn",
        ".btn--primary",
        ".btn--secondary",
        ".btn--ghost",
        ".btn--danger",
        ".btn--accent-soft",
        ".pill",
        ".pill--movie",
        ".pill--tv",
        ".pill--anime",
        ".pill--kept",
        ".pill--stale",
        ".pill--queued",
        ".pill--neutral",
        ".fpill",
        ".fpill--movie",
        ".fpill--tv",
        ".card",
        ".card--bordered",
        ".watch-all__filters",
        ".inp",
        ".setg-card",
        ".setg-row",
        ".tog",
        ".conn",
        ".dl-hero",
        ".dl-row",
        ".dl-state-pill",
        ".hist",
        ".empty",
        ".eyebrow",
        ".modal-backdrop",
        ".modal-sheet",
    )
    for sel in primitives:
        assert sel in css, f"missing primitive: {sel}"


def test_single_shadow_token():
    """Only one card-lift shadow is permitted; --shadow-card defines it.

    Decorative glows (status dots, focus rings) are allowed because they
    aren't elevation — they're light effects on the same plane. Every layer
    of a shadow is held to this, not only its first.
    """
    css = _css()
    assert "--shadow-card:" in css
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
    tokens = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", css))
    # Every value a token takes, in every condition it is set in.
    every: dict[str, list[str]] = {}
    for name, value in re.findall(r"(--[\w-]+):\s*([^;]+);", css):
        every.setdefault(name, []).append(value)
    for selector, value in (
        (sel, v) for sel, body in rules for v in re.findall(r"box-shadow:\s*([^;]+);", body)
    ):
        v = " ".join(value.split())
        if v in ("none", "inherit"):
            continue
        token = re.fullmatch(r"var\((--glow-[\w-]+)\)", v)
        if token:
            # A shadow token is held to the same rule as a literal, layer by layer: no offset
            # and a blur of at most 12px. The AI ring's light is the one wider glow; it may
            # spread sideways but never drops, so no layer of it has a y offset.
            for layer in _shadow_layers(tokens[token.group(1)]):
                if token.group(1) == "--glow-ai":
                    assert re.match(r"-?\d+(px)? 0 \d+px ", layer), (
                        f"--glow-ai layer drops: {layer!r}"
                    )
                else:
                    assert re.match(r"0 0 ([0-9]|1[0-2])(px)? ", layer), (
                        f"{token.group(1)} layer is not a small glow: {layer!r}"
                    )
            continue
        layers = _shadow_layers(v)
        if "var(--g-inset)" in layers:
            # Glass's lit top edge: a line of light along the edge, with no blur, lifts nothing.
            for each in every["--g-inset"]:
                assert re.fullmatch(
                    r"inset 0 1px 0 \S.*|0 0 0 transparent", " ".join(each.split())
                ), f"--g-inset is more than a line of light: {each!r}"
            v = ", ".join(layer for layer in layers if layer != "var(--g-inset)")
            if not v:
                continue
        for layer in _resolved_layers(v, every):
            if layer == "var(--shadow-card)":
                continue
            # Status-dot / focus-ring style glows and hairline rings: zero offset. These are
            # light effects, not elevation.
            is_glow = bool(re.match(r"(inset )?0 0 \d+(px)? ", layer))
            # The dl-hero poster uses a single bespoke deep drop because the
            # backdrop blur already provides the room-level lift.
            is_hero_poster = "rgba(0,0,0,.6) 0 20px 40px" in layer
            # A field a browser filled in is painted over in its own surface colour with an
            # inset spread: no offset, no blur, so nothing lifts.
            is_autofill_paint = ":autofill" in selector and bool(
                re.fullmatch(r"inset 0 0 0 99px var\(--s[23]\)", layer)
            )
            assert is_glow or is_hero_poster or is_autofill_paint, (
                f"unexpected box-shadow layer: {layer!r} in {v!r}"
            )


def _resolved_layers(value: str, every: dict[str, list[str]]) -> list[str]:
    """A box-shadow's layers, with a layer that is a token (other than the card's own shadow)
    replaced by the layers of every value the token takes."""
    out = []
    for layer in _shadow_layers(value):
        token = re.fullmatch(r"var\((--[\w-]+)\)", layer)
        if token and token.group(1) != "--shadow-card" and token.group(1) in every:
            for each in every[token.group(1)]:
                out += _resolved_layers(each, every)
        else:
            out.append(layer)
    return out


def _shadow_layers(value: str) -> list[str]:
    """A box-shadow's layers: split at the commas that are not inside a function's brackets."""
    layers, depth, start = [], 0, 0
    value = " ".join(value.split())
    for i, ch in enumerate(value):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            layers.append(value[start:i].strip())
            start = i + 1
    return [*layers, value[start:].strip()]


def test_the_ai_search_glow_is_drawn_from_its_own_token():
    assert "--glow-ai:" in _css()
    assert re.search(r"\.wbar \.watch-search::after \{[^}]*box-shadow: var\(--glow-ai\);", _css())


def test_nav_template_emits_all_surfaces():
    nav = _tpl("_nav.html")
    for cls in (
        "nav-glass",
        "nav-back",
        "nav-end",
        "profile-btn",
        "profile-ring",
        "profile-count",
        "profile-menu",
        "profile-item",
    ):
        assert cls in nav, f"missing nav surface class: {cls}"
    # Every primary destination still routes from the nav.
    for href in (
        'href="/"',
        'href="/?dl=1"',
        'href="/history"',
        'href="/settings"',
    ):
        assert href in nav, f"missing nav link: {href}"
    assert "}}Watch<" in nav, "missing nav label: Watch"
    # Downloads left the profile menu; without scripts Downloading on Watch is still a link of
    # the bar
    assert 'href="/?dl=1">Downloads<' in nav
    menu = nav.split('id="profile-menu-items"')[1].split("</div>")[0]
    assert "dl=1" not in menu and "/downloads" not in menu and "Downloads" not in menu


def test_base_html_sets_data_page():
    base = _tpl("base.html")
    assert 'data-page="{{ nav_active' in base


def test_components_macros_importable():
    """The macros file is the documented composition layer."""
    components = _tpl("_components.html")
    for macro in (
        "macro btn(",
        "macro pill(",
        "macro fpill(",
        "macro setg_card(",
        "macro setg_row(",
        "macro tog(",
        "macro inp(",
        "macro conn(",
        "macro empty(",
        "macro eyebrow(",
    ):
        assert macro in components, f"missing macro: {macro}"


def test_every_page_uses_default_container():
    """Every authed page composes inside the default .container so headings
    and content rails line up across screens. Using `--narrow` or
    `--wide` modifiers is forbidden after the width-consistency pass."""
    history = _tpl("panes/_history.html")
    # Every page must mount its content inside .container (no modifier).
    assert 'class="container"' in history, "panes/_history.html: missing default .container"
    # And must not silently re-introduce the legacy modifiers.
    for legacy in ("container--narrow", "container--wide"):
        assert legacy not in history, f"panes/_history.html: legacy {legacy} re-introduced"

    # The Settings pane lays its list and sections out on its own .stx rail, full width in its view.
    settings = _tpl("panes/_settings.html")
    assert '<div class="setg-pg stx" data-stx>' in settings, (
        "panes/_settings.html: missing .stx rail"
    )
    for legacy in ("container--narrow", "container--wide"):
        assert legacy not in settings, f"panes/_settings.html: legacy {legacy} re-introduced"

    # The Watch page lays itself out on its own .watch-page rail; it must not
    # reach for the legacy container modifiers either.
    watch = _tpl("watch.html")
    assert 'class="watch-page"' in watch, "watch.html: missing .watch-page rail"
    for legacy in ("container--narrow", "container--wide"):
        assert legacy not in watch, f"watch.html: legacy {legacy} re-introduced"
