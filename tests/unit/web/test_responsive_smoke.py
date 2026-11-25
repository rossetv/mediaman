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


def test_container_system_present():
    css = _css()
    for cls in (".container", ".container--narrow", ".container--wide"):
        assert cls in css, f"missing container modifier: {cls}"


def test_nav_surfaces_present():
    """The nav is one bar, .nav-glass, the strip at the foot of a phone, .nav-foot, on which
    the way back (or the search) and the profile button stand, and the .profile-menu."""
    css = _css()
    for sel in (
        ".nav-glass",
        ".nav-back",
        ".nav-foot",
        ".profile-btn",
        ".profile-ring",
        ".profile-count",
        ".profile-menu",
        ".profile-panel",
        ".profile-item",
    ):
        assert sel in css, f"missing nav selector: {sel}"


def test_bottom_bar_uses_safe_area():
    heights = re.findall(r"--foot: ([^;]+);", _css())
    tall = [h for h in heights if h != "0px"]
    assert len(tall) == 2
    assert all("env(safe-area-inset-bottom" in h for h in tall)


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
    aren't elevation — they're light effects on the same plane.
    """
    css = _css()
    assert "--shadow-card:" in css
    box_shadows = re.findall(r"box-shadow:\s*([^;]+);", css)
    tokens = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", css))
    for value in box_shadows:
        v = value.strip()
        # A shadow token is held to the same rule as a literal.
        v = re.sub(r"^var\((--glow-[\w-]+)\)$", lambda m: tokens[m.group(1)].strip(), v)
        if "var(--shadow-card)" in v or v in ("none", "inherit"):
            continue
        # Status-dot / focus-ring style glows: small radius (≤12px) with
        # zero offset. These are light effects, not elevation.
        is_glow = bool(re.match(r"0 0 \d+px ", v))
        # The dl-hero poster uses a single bespoke deep drop because the
        # backdrop blur already provides the room-level lift.
        is_hero_poster = "rgba(0,0,0,.6) 0 20px 40px" in v
        assert is_glow or is_hero_poster, f"unexpected box-shadow literal: {v!r}"


def test_nav_template_emits_all_surfaces():
    nav = _tpl("_nav.html")
    for cls in (
        "nav-glass",
        "nav-back",
        "nav-foot",
        "profile-btn",
        "profile-ring",
        "profile-count",
        "profile-menu",
        "profile-item",
    ):
        assert cls in nav, f"missing nav surface class: {cls}"
    # Every primary destination still routes from the nav.
    for href in (
        'href="/watch"',
        'href="/downloads"',
        'href="/history"',
        'href="/settings"',
    ):
        assert href in nav, f"missing nav link: {href}"
    for label in ("}}Watch<", "}}Downloads<"):
        assert label in nav, f"missing nav label: {label}"


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
    for name in (
        "downloads.html",
        "history.html",
        "settings.html",
    ):
        tpl = _tpl(name)
        # Every page must mount its content inside .container (no modifier).
        assert 'class="container"' in tpl, f"{name}: missing default .container"
        # And must not silently re-introduce the legacy modifiers.
        for legacy in ("container--narrow", "container--wide"):
            assert legacy not in tpl, f"{name}: legacy {legacy} re-introduced"

    # The Watch page lays itself out on its own .watch-page rail; it must not
    # reach for the legacy container modifiers either.
    watch = _tpl("watch.html")
    assert 'class="watch-page"' in watch, "watch.html: missing .watch-page rail"
    for legacy in ("container--narrow", "container--wide"):
        assert legacy not in watch, f"watch.html: legacy {legacy} re-introduced"
