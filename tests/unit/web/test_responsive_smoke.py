"""Smoke tests for the responsive redesign.

These tests assert that the CSS and key templates contain the
primitives introduced by the responsive-UI redesign. They do not
render HTML or exercise routes — they are guardrails so that a
template refactor doesn't silently remove the new classes.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CSS = REPO / "src/mediaman/web/static/style.css"
TEMPLATES = REPO / "src/mediaman/web/templates"


def _css() -> str:
    return CSS.read_text(encoding="utf-8")


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_responsive_tokens_defined():
    css = _css()
    for token in ("--bp-sm", "--bp-md", "--bp-lg", "--bp-xl",
                  "--container-pad", "--space-section", "--space-card",
                  "--page-header-mb", "--nav-h", "--tab-h"):
        assert token in css, f"missing token: {token}"


def test_fluid_type_uses_clamp():
    css = _css()
    # Each of these rules must use clamp() for its font-size so text
    # scales smoothly between viewports. The full selector + "clamp("
    # check catches silent reverts to fixed px.
    patterns = (
        ".page-header h1 {\n  font-size: clamp(",
        ".page-header p {\n  font-size: clamp(",
        ".section-title {\n  font-size: clamp(",
        ".lib-stat-value {\n  font-size: clamp(",
    )
    for p in patterns:
        assert p in css, f"expected fluid font-size: {p!r}"


def test_container_system_present():
    css = _css()
    for cls in (".container--narrow", ".container--wide", ".container--fluid"):
        assert cls in css, f"missing container modifier: {cls}"
    # Legacy aliases removed in Task 18.
    assert ".container-wide {" not in css
    assert ".container-narrow {" not in css


def test_container_reserves_tab_bar_space_on_mobile():
    """The base .container rule must reserve bottom space for the tab
    bar + safe-area. Verified by finding the calc inside the .container
    selector, not just anywhere in the file."""
    import re
    css = _css()
    match = re.search(r'\.container\s*\{([^}]*)\}', css)
    assert match is not None, ".container rule not found"
    block = match.group(1)
    assert "env(safe-area-inset-bottom" in block, \
        ".container must use env(safe-area-inset-bottom) in padding-bottom"
    assert "var(--tab-h)" in block, \
        ".container must reference var(--tab-h)"


def test_nav_surfaces_present():
    css = _css()
    for sel in (".nav-top", ".nav-topbar", ".nav-tabs", ".nav-tab",
                ".nav-more-sheet"):
        assert sel in css, f"missing nav selector: {sel}"


def test_bottom_tab_bar_uses_safe_area():
    css = _css()
    idx = css.find(".nav-tabs {")
    assert idx >= 0
    block = css[idx:idx + 1200]
    assert "env(safe-area-inset-bottom" in block


def test_shared_patterns_present():
    css = _css()
    # .stacked-row is a documented helper but not every consumer adopts
    # it (library uses .lib-row, history uses .history-row, etc.); the
    # assertion checks for the primitives that every page depends on.
    for sel in (".form-row", ".filter-pills", ".cards-grid", ".poster-grid",
                ".modal-sheet", ".modal-backdrop", ".toolbar-row"):
        assert sel in css, f"missing shared pattern: {sel}"


def test_filter_pills_horizontal_scroll():
    css = _css()
    idx = css.find(".filter-pills {")
    assert idx >= 0
    block = css[idx:idx + 600]
    assert "overflow-x: auto" in block
    assert "scrollbar-width: none" in block


def test_nav_template_emits_all_three_surfaces():
    nav = _tpl("_nav.html")
    assert "nav-top" in nav
    assert "nav-topbar" in nav
    assert "nav-tabs" in nav
    assert "nav-more-sheet" in nav
    # All five bottom tabs must be present.
    for label in ("Home", "Library", "Search", "Downloads", "More"):
        assert f">{label}<" in nav or f">{label}</" in nav, f"missing tab label: {label}"


def test_base_html_sets_data_page():
    base = _tpl("base.html")
    assert 'data-page="{{ nav_active' in base


def test_library_uses_container_wide_modifier():
    tpl = _tpl("library.html")
    assert 'container container--wide' in tpl or 'container--wide' in tpl
