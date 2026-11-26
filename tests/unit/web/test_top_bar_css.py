"""The top bar's own details: the logo moves with its island, answers a pointer, and the field leaves the keyboard its way."""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "static" / "css"


def _css(name: str) -> str:
    return re.sub(r"/\*.*?\*/", "", (CSS / name).read_text(encoding="utf-8"), flags=re.S)


def _body(selector: str, css: str) -> str:
    """The declarations of every rule whose selector list holds exactly *selector*."""
    return "".join(
        body
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if selector in [s.strip() for s in head.split(",")]
    )


def test_the_logo_slides_with_its_island_and_only_its_fade_back_waits() -> None:
    brand = _body(".wbar .brand", _css("_watch_search.css"))
    transform = re.search(r"transform ([^,;]+)", brand).group(1)  # type: ignore[union-attr]
    assert not re.search(r"\.\d+s", transform)
    assert re.search(r"opacity [^,;]+ \.12s", brand)
    # the storage and the profile still wait their turn as a whole
    shared = re.search(
        r"\.wbar :is\(\.space-meter, \.nav-end\) \{([^{}]*)\}", _css("_watch_search.css")
    )
    assert shared and "transform var(--dur-slow) var(--ease-out) .12s" in shared.group(1)


def test_the_logo_dims_under_a_pointer_and_more_when_pressed() -> None:
    css = _css("_nav.css")
    assert "color:" in _body(".nav-glass .brand:hover", css)
    assert "color:" in _body(".nav-glass .brand:active", css)
    assert "color var(--dur-fast)" in _body(".nav-glass .brand", css)


def test_a_focused_empty_field_leaves_the_logo_and_the_storage_where_tab_reaches_them() -> None:
    css = _css("_watch_search.css")
    hidden = [h for h, b in re.findall(r"([^{}]+)\{([^{}]*)\}", css) if "display: none" in b]
    rules = [h for h in hidden if "> .brand" in h or "> .space-meter" in h]
    assert rules
    for head in rules:
        assert ".is-focus:not(.has-value, .has-token)" in head


def test_settings_scrolls_nothing_for_a_step_back_into_the_bar() -> None:
    assert "scroll-padding-top: 0" in _body(
        "html.has-setg-pg.is-bar-focus", _css("_settings_pg.css")
    )


def test_no_sheet_leans_on_a_height_at_the_foot_that_is_never_set() -> None:
    assert [p.name for p in CSS.glob("*.css") if "--foot" in p.read_text(encoding="utf-8")] == []
